"""Prowlarr indexer + qBittorrent download client for DroppedNeedle.

Search: ``search_album`` queries Prowlarr's torrent indexers and returns each
release with the magnet link / .torrent URL as its payload, scored and tiered
so DroppedNeedle's own quality settings and import pipeline apply on top.

Download: sends the torrent or magnet link an indexer supplies (``EnqueueRequest.payload``)
to qBittorrent's Web API v2, always setting the configured category (default
``music``). Torrents are identified by a per-task tag, so the plugin keeps no
state and survives restarts. Folder mode: the finished files are discovered
from qBittorrent when the torrent completes.
"""

import asyncio
import base64
import hashlib
import json
import re
import unicodedata
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from droppedneedle_plugin import (
    DownloadFailure,
    DownloadMaterialization,
    DownloadTaskStatus,
    EnqueueRequest,
    IndexerResult,
    MountDiagnosis,
    PluginSearchResult,
    ServiceStatus,
    TaskHandle,
)

DEFAULT_CATEGORY = "music"
SOURCE = "plugin:qbittorrent"
APPEAR_TIMEOUT = 15.0

# qBittorrent states (v4 and v5 names) grouped by what they mean to us.
_FAILED_LOCAL = {"missingFiles"}
_FAILED_UNKNOWN = {"error"}
_QUEUED = {"queuedDL", "checkingResumeData", "allocating"}
_MOVING = {"moving"}
_BTIH = re.compile(r"xt=urn:btih:([^&]+)", re.IGNORECASE)

DEFAULT_PROWLARR_CATEGORIES = "3000"  # Newznab "Audio"
MAX_RESULTS = 100
# Checked in order; first hit wins.
_TIERS = (
    ("lossless", re.compile(r"flac|alac|lossless|\bwav\b|\bape\b|\bdsd|24[ -]?bit", re.I)),
    ("mp3_320", re.compile(r"\b320\b", re.I)),
    ("mp3_256", re.compile(r"\bv0\b|\b256\b", re.I)),
    ("mp3_192", re.compile(r"\bv2\b|\b192\b", re.I)),
    ("low", re.compile(r"\b(128|96|64)\b", re.I)),
)


_STOPWORDS = {"the", "a", "an", "and", "of"}
_COLLECTION = re.compile(r"discograph|collection|complete|anthology|\bbox ?set\b|\bbest of\b|greatest hits", re.I)


def _words(text: str, *, drop_stopwords: bool = False) -> set[str]:
    """Lowercase, accent-free, '&'='and', apostrophes dropped."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    text = text.replace("&", " and ").replace("'", "").replace("\u2019", "")
    words = {w for w in re.split(r"[^0-9a-z\u0400-\uffff]+", text) if w}
    return words - _STOPWORDS if drop_stopwords and words - _STOPWORDS else words


def score_release(artist: str, album: str, title: str, year: int | None = None) -> float:
    """0-1 confidence that a release title is this artist's album.

    Edition tags in the wanted album ("(Deluxe Edition)") are ignored, the
    album counts more than the artist, and multi-album collections are
    marked down when the wanted album is not itself one.
    """
    core_album = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", album or "") or album
    found = _words(title)
    wanted_artist = _words(artist, drop_stopwords=True)
    wanted_album = _words(core_album, drop_stopwords=True)

    def coverage(wanted: set[str]) -> float:
        return len(wanted & found) / len(wanted) if wanted else 1.0

    if not wanted_artist and not wanted_album:
        return 0.0
    score = 0.4 * coverage(wanted_artist) + 0.6 * coverage(wanted_album)
    if _COLLECTION.search(title or "") and not _COLLECTION.search(album or ""):
        score *= 0.6
    if year and str(year) in (title or ""):
        score += 0.05
    return round(min(score, 1.0), 4)


REF_PREFIX = "prowlarr:"


def encode_ref(release: dict) -> str:
    """A payload that is the same on every search. Prowlarr's own download URLs
    are not (each carries a fresh encrypted token), and DroppedNeedle re-finds
    a picked release by its payload."""
    ref = {"g": release.get("guid") or "", "i": release.get("indexerId"), "t": release.get("title") or ""}
    raw = json.dumps(ref, separators=(",", ":"), sort_keys=True).encode()
    return REF_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_ref(payload: str) -> dict:
    raw = payload[len(REF_PREFIX):]
    return json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))


def _bencode_end(data: bytes, pos: int) -> int:
    """Index just past the bencoded value starting at ``pos``."""
    kind = data[pos : pos + 1]
    if kind == b"i":
        return data.index(b"e", pos) + 1
    if kind in (b"l", b"d"):
        pos += 1
        while data[pos : pos + 1] != b"e":
            pos = _bencode_end(data, pos)
        return pos + 1
    colon = data.index(b":", pos)
    return colon + 1 + int(data[pos:colon])


def torrent_info_hash(data: bytes) -> str:
    """Hex SHA-1 of a .torrent file's info dictionary, or '' if unreadable."""
    try:
        if data[:1] != b"d":
            return ""
        pos = 1
        while data[pos : pos + 1] != b"e":
            key_end = _bencode_end(data, pos)
            key = data[data.index(b":", pos) + 1 : key_end]
            value_end = _bencode_end(data, key_end)
            if key == b"info":
                return hashlib.sha1(data[key_end:value_end]).hexdigest()
            pos = value_end
    except (ValueError, IndexError):
        pass
    return ""


def _multipart(fields: dict, file_field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        "Content-Type: application/x-bittorrent\r\n\r\n".encode() + content + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _query_variants(artist: str, album: str) -> list[str]:
    """Search strings, most specific first: 'artist album', then the album with
    edition tags / punctuation removed ('Album (Deluxe Edition)' -> 'Album')."""
    cleaned = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", album)
    cleaned = re.sub(r"[^\w\s]", " ", cleaned)
    variants = []
    for album_part in (album, " ".join(cleaned.split())):
        query = f"{artist} {album_part}".strip()
        if query and query not in variants:
            variants.append(query)
    return variants


def quality_tier(title: str) -> str:
    """Best-effort audio quality tier from a release title, '' when unknown."""
    for tier, pattern in _TIERS:
        if pattern.search(title or ""):
            return tier
    return ""


def _text(value) -> str:
    return (value or "").strip() if isinstance(value, str) else ""


class QBittorrent:
    def __init__(self, ctx):
        self.ctx = ctx
        self._cookie: str | None = None  # "NAME=value" session cookie
        self._login_lock = asyncio.Lock()

    # -- settings -------------------------------------------------------

    def _setting(self, key: str) -> str:
        return _text(self.ctx.settings.get(key))

    def _base_url(self) -> str:
        url = self._setting("url").rstrip("/")
        if url and "://" not in url:
            url = "http://" + url
        return url

    def _category(self) -> str:
        return self._setting("category") or DEFAULT_CATEGORY

    def is_configured(self) -> bool:
        return bool(self._base_url())

    def _prowlarr_url(self) -> str:
        url = self._setting("prowlarr_url").rstrip("/")
        if url and "://" not in url:
            url = "http://" + url
        return url

    def _min_seeders(self) -> int:
        try:
            return max(0, int(self._setting("min_seeders") or 1))
        except ValueError:
            return 1

    # -- HTTP -----------------------------------------------------------

    def _auth_headers(self) -> dict:
        """API key (Bearer) when configured, else the session cookie from login."""
        api_key = self._setting("api_key")
        if api_key:
            return {"Authorization": f"Bearer {api_key}"}
        return {"Cookie": self._cookie} if self._cookie else {}

    async def _login(self) -> None:
        base = self._base_url()
        if not base:
            raise DownloadFailure("qBittorrent URL is not configured", fault="local")
        if self._setting("api_key"):
            return  # API keys need no login (and /auth/* rejects them)
        try:
            response = await self.ctx.http.post(
                f"{base}/api/v2/auth/login",
                data={"username": self._setting("username"), "password": self._setting("password")},
                headers={"Referer": base},
                timeout=15.0,
            )
        except OSError as exc:
            raise DownloadFailure(f"cannot reach qBittorrent at {base}: {exc}", fault="local") from exc
        if response.status_code == 403:
            raise DownloadFailure("qBittorrent refused the login (IP banned after failed attempts?)", fault="local")
        if response.status_code != 200 or response.text.strip().lower().startswith("fails"):
            raise DownloadFailure(
                f"qBittorrent login failed (HTTP {response.status_code}: {response.text.strip()[:60]!r}); "
                "check the username and password, or use an API key",
                fault="local",
            )
        self._cookie = None
        for key, value in response.headers.items():
            if key.lower() == "set-cookie":
                # "SID" on v4, "QBT_SID_<port>" on v5.
                match = re.match(r"\s*((?:QBT_)?SID[A-Za-z0-9_]*=[^;]+)", value)
                if match:
                    self._cookie = match.group(1)
        # No cookie with an "Ok." answer means authentication is bypassed.

    async def _request(
        self, method: str, path: str, *, params=None, data=None, content=None, extra_headers=None, timeout: float = 30.0
    ):
        base = self._base_url()
        if not base:
            raise DownloadFailure("qBittorrent URL is not configured", fault="local")
        async with self._login_lock:
            if self._cookie is None:
                await self._login()
        for attempt in (1, 2):
            headers = {"Referer": base, **self._auth_headers(), **(extra_headers or {})}
            try:
                response = await self.ctx.http.request(
                    method,
                    f"{base}/api/v2/{path}",
                    params=params,
                    data=data,
                    content=content,
                    headers=headers,
                    timeout=timeout,
                )
            except OSError as exc:
                raise DownloadFailure(f"cannot reach qBittorrent at {base}: {exc}", fault="local") from exc
            if response.status_code == 403 and attempt == 1 and not self._setting("api_key"):
                async with self._login_lock:
                    await self._login()
                continue
            if response.status_code in (401, 403) and self._setting("api_key"):
                raise DownloadFailure("qBittorrent rejected the API key", fault="local")
            return response
        return response  # pragma: no cover

    async def _json(self, path: str, **params):
        response = await self._request("GET", path, params=params or None)
        if response.status_code != 200:
            raise DownloadFailure(f"qBittorrent {path} answered HTTP {response.status_code}", fault="unknown")
        return response.json()

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _tag(task_id: str) -> str:
        return "droppedneedle-" + re.sub(r"[^A-Za-z0-9_.-]", "_", task_id or "")

    @staticmethod
    def _magnet_hash(payload: str) -> str:
        """Info-hash (hex, lowercase) of a magnet link, or ''."""
        match = _BTIH.search(payload or "")
        if not match:
            return ""
        value = match.group(1)
        if len(value) == 40:
            return value.lower()
        if len(value) == 32:
            try:
                return base64.b32decode(value.upper()).hex()
            except ValueError:
                return ""
        return ""

    async def _ensure_category(self, category: str) -> None:
        known = await self._json("torrents/categories")
        if category not in known:
            response = await self._request("POST", "torrents/createCategory", data={"category": category, "savePath": ""})
            if response.status_code not in (200, 409):
                self.ctx.logger.warning("could not create category %r: HTTP %s", category, response.status_code)

    async def _find(self, handle: TaskHandle) -> dict | None:
        tag = (handle.job_name or "").strip()
        if not tag:
            return None
        found = await self._json("torrents/info", tag=tag)
        return found[0] if found else None

    def _local_path(self, remote: str) -> Path:
        """Translate a path as qBittorrent sees it into DroppedNeedle's view."""
        remote = (remote or "").replace("\\", "/")
        remote_root = self._setting("remote_path").replace("\\", "/").rstrip("/")
        local_root = self._setting("downloads_dir")
        if remote_root and local_root:
            if remote == remote_root:
                return Path(local_root)
            if remote.startswith(remote_root + "/"):
                return Path(local_root) / remote[len(remote_root) + 1 :]
        return Path(remote)

    # -- health ---------------------------------------------------------

    async def health_check(self) -> ServiceStatus:
        if not self._base_url():
            return ServiceStatus(status="error", message="qBittorrent URL is not configured")
        try:
            async with self._login_lock:
                await self._login()
            response = await self._request("GET", "app/version", timeout=15.0)
        except DownloadFailure as exc:
            return ServiceStatus(status="error", message=str(exc))
        if response.status_code != 200:
            return ServiceStatus(status="error", message=f"qBittorrent answered HTTP {response.status_code}")
        downloads = self._setting("downloads_dir")
        if downloads and not Path(downloads).is_dir():
            return ServiceStatus(
                status="error",
                message=f"downloads directory not found: {downloads}",
                version=response.text.strip(),
            )
        if self._prowlarr_url():
            problem = await self._prowlarr_problem()
            if problem:
                return ServiceStatus(status="error", message=problem, version=response.text.strip())
        return ServiceStatus(status="ok", version=response.text.strip())

    async def _prowlarr_problem(self) -> str:
        """'' when Prowlarr answers with the configured API key, else why not."""
        try:
            response = await self.ctx.http.get(
                f"{self._prowlarr_url()}/api/v1/system/status",
                headers={"X-Api-Key": self._setting("prowlarr_api_key")},
                timeout=10.0,
            )
        except OSError as exc:
            return f"cannot reach Prowlarr at {self._prowlarr_url()}: {exc}"
        if response.status_code in (401, 403):
            return "Prowlarr rejected the API key"
        if response.status_code != 200:
            return f"Prowlarr answered HTTP {response.status_code}"
        return ""

    # -- indexer (Prowlarr) ---------------------------------------------

    async def _prowlarr_search(self, query: str, categories: list[str], timeout: float) -> list[dict]:
        params = {"query": query, "type": "search"}
        if categories:
            params["categories"] = categories
        response = await self.ctx.http.get(
            f"{self._prowlarr_url()}/api/v1/search",
            params=params,
            headers={"X-Api-Key": self._setting("prowlarr_api_key")},
            timeout=timeout,
        )
        if response.status_code in (401, 403):
            raise RuntimeError("Prowlarr rejected the API key")
        if response.status_code != 200:
            raise RuntimeError(f"Prowlarr answered HTTP {response.status_code}")
        found = response.json()
        return found if isinstance(found, list) else []

    async def _search_variants(self, queries: list[str], categories: list[str], timeout: float) -> list[dict]:
        """Run the queries concurrently; one failing query does not sink the others."""
        outcomes = await asyncio.gather(
            *(self._prowlarr_search(q, categories, timeout) for q in queries), return_exceptions=True
        )
        merged, seen = [], set()
        for query, outcome in zip(queries, outcomes):
            if isinstance(outcome, BaseException):
                self.ctx.logger.warning("Prowlarr search %r failed: %s", query, outcome)
                continue
            self.ctx.logger.info("Prowlarr search %r (categories=%s): %d raw result(s)", query, categories or "any", len(outcome))
            for release in outcome:
                key = release.get("guid") or release.get("magnetUrl") or release.get("downloadUrl") or id(release)
                if key not in seen:
                    seen.add(key)
                    merged.append(release)
        return merged

    async def search_album(
        self,
        artist_name: str,
        album_title: str,
        year: int | None = None,
        track_count: int | None = None,
        *,
        timeout: float = 30.0,
    ) -> list[IndexerResult]:
        if not self._prowlarr_url() or not self._setting("prowlarr_api_key"):
            self.ctx.logger.info("Prowlarr is not configured; skipping search")
            return []
        artist, album = (artist_name or "").strip(), (album_title or "").strip()
        queries = _query_variants(artist, album)
        if not queries:
            return []
        categories = [
            c.strip()
            for c in (self._setting("prowlarr_categories") or DEFAULT_PROWLARR_CATEGORIES).split(",")
            if c.strip()
        ]
        try:
            async with asyncio.timeout(timeout):
                releases = await self._search_variants(queries, categories, timeout)
                if not releases and categories:
                    # Some indexers file music under odd categories: retry unfiltered.
                    releases = await self._search_variants(queries, [], timeout)
        except TimeoutError:
            self.ctx.logger.warning(
                "Prowlarr search for %r timed out after %.0fs (slow indexers? disable them in Prowlarr)", queries[0], timeout
            )
            return []
        except Exception as exc:  # noqa: BLE001 - one bad search is [], never a crash
            self.ctx.logger.warning("Prowlarr search failed: %s", exc)
            return []

        min_seeders = self._min_seeders()
        scored = []
        skipped = {"usenet": 0, "no link": 0, "seeders": 0}
        for release in releases:
            if str(release.get("protocol") or "torrent").lower() != "torrent":
                skipped["usenet"] += 1
                continue
            magnet = release.get("magnetUrl") or ""
            if not magnet and not release.get("downloadUrl"):
                skipped["no link"] += 1
                continue
            link = magnet or encode_ref(release)
            # Some indexers report no seeder count at all: keep those (unknown, not zero).
            seeders = release.get("seeders")
            if seeders is not None and int(seeders) < min_seeders:
                skipped["seeders"] += 1
                continue
            title = str(release.get("title") or "")
            score = score_release(artist, album, title, year)
            scored.append(
                (
                    score,
                    int(seeders or 0),
                    PluginSearchResult(
                        title=title,
                        size_bytes=int(release.get("size") or 0),
                        score=round(score, 4),
                        quality_tier=quality_tier(title),
                        payload=link,
                    ),
                )
            )
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        self.ctx.logger.info(
            "Prowlarr: %d release(s) -> %d usable for %r (skipped: %s); best: %s",
            len(releases), len(scored), f"{artist} - {album}", skipped,
            "; ".join(f"{item[0]:.2f} {item[2].title[:70]}" for item in scored[:3]) or "none",
        )
        return [IndexerResult(source=SOURCE, plugin=item[2]) for item in scored[:MAX_RESULTS]]

    async def search_track(
        self,
        artist_name: str,
        track_title: str,
        album_title: str | None = None,
        duration_seconds: int | None = None,
        *,
        timeout: float = 30.0,
    ) -> list[IndexerResult]:
        return []  # torrents are albums; single tracks are not searched

    # -- download client ------------------------------------------------

    async def _resolve_ref(self, payload: str) -> tuple[str, bytes | None]:
        """Turn a Prowlarr reference into (magnet, None) or ("", .torrent bytes)."""
        try:
            ref = decode_ref(payload)
        except (ValueError, json.JSONDecodeError) as exc:
            raise DownloadFailure("unreadable Prowlarr reference in the result payload", fault="source") from exc
        if not self._prowlarr_url():
            raise DownloadFailure("Prowlarr is not configured", fault="local")
        try:
            releases = await self._prowlarr_search(ref.get("t") or "", [], 30.0)
        except Exception as exc:  # noqa: BLE001
            raise DownloadFailure(f"Prowlarr lookup failed: {exc}", fault="local") from exc
        match = next(
            (r for r in releases if (ref.get("g") and r.get("guid") == ref["g"])),
            None,
        ) or next(
            (r for r in releases if r.get("title") == ref.get("t") and r.get("indexerId") == ref.get("i")),
            None,
        )
        if match is None:
            raise DownloadFailure("Prowlarr no longer lists this release", fault="source")
        if match.get("magnetUrl"):
            return match["magnetUrl"], None
        try:
            response = await self.ctx.http.get(
                match["downloadUrl"],
                headers={"X-Api-Key": self._setting("prowlarr_api_key")},
                timeout=60.0,
            )
        except OSError as exc:
            # A redirect to a magnet: link cannot be followed by urllib.
            raise DownloadFailure(f"could not fetch the .torrent from Prowlarr: {exc}", fault="unknown") from exc
        if response.status_code != 200 or response.content[:1] != b"d":
            raise DownloadFailure(
                f"Prowlarr did not return a torrent file (HTTP {response.status_code})", fault="source"
            )
        return "", response.content

    async def enqueue(self, request: EnqueueRequest) -> TaskHandle:
        payload = (request.payload or "").strip()
        is_ref = payload.startswith(REF_PREFIX)
        if not payload or not (
            is_ref or payload.lower().startswith("magnet:") or urlsplit(payload).scheme in ("http", "https")
        ):
            raise DownloadFailure(
                "qBittorrent needs a magnet link or .torrent URL in the result payload", fault="source"
            )
        category = self._category()
        tag = self._tag(request.task_id)
        link, torrent = payload, None
        if is_ref:
            link, torrent = await self._resolve_ref(payload)
        await self._ensure_category(category)

        if torrent is not None:
            body, content_type = _multipart(
                {"category": category, "tags": tag}, "torrents", "release.torrent", torrent
            )
            response = await self._request(
                "POST", "torrents/add", content=body, extra_headers={"Content-Type": content_type}, timeout=60.0
            )
            info_hash = torrent_info_hash(torrent)
        else:
            response = await self._request(
                "POST", "torrents/add", data={"urls": link, "category": category, "tags": tag}, timeout=60.0
            )
            info_hash = self._magnet_hash(link)
        body = response.text.strip()
        if response.status_code == 415:
            raise DownloadFailure("qBittorrent rejected the torrent file", fault="source")
        if response.status_code != 200:
            raise DownloadFailure(f"qBittorrent answered HTTP {response.status_code} to add", fault="unknown")
        if body.lower().startswith("fails"):
            # Most likely already in qBittorrent: adopt it instead of failing.
            if not info_hash or not await self._json("torrents/info", hashes=info_hash):
                raise DownloadFailure("qBittorrent could not add the torrent (invalid link?)", fault="source")
            await self._request("POST", "torrents/addTags", data={"hashes": info_hash, "tags": tag})
            await self._request("POST", "torrents/setCategory", data={"hashes": info_hash, "category": category})

        handle = TaskHandle(source=SOURCE, job_name=tag, plugin_token=payload)
        deadline = asyncio.get_running_loop().time() + APPEAR_TIMEOUT
        while await self._find(handle) is None:
            if asyncio.get_running_loop().time() >= deadline:
                raise DownloadFailure("torrent did not appear in qBittorrent after adding", fault="unknown")
            await asyncio.sleep(0.5)
        self.ctx.logger.info("queued %s in category %r", tag, category)
        return handle

    async def get_status(self, handle: TaskHandle) -> DownloadTaskStatus:
        task_id = (handle.job_name or "").removeprefix("droppedneedle-")
        info = await self._find(handle)
        if info is None:
            return DownloadTaskStatus(task_id=task_id, status="failed", error="torrent no longer in qBittorrent")
        state = info.get("state", "")
        progress = float(info.get("progress", 0.0))
        size = int(info.get("size") or info.get("total_size") or 0)
        if state in _FAILED_LOCAL:
            raise DownloadFailure(f"qBittorrent: {state} (files missing on disk)", fault="local")
        if state in _FAILED_UNKNOWN:
            raise DownloadFailure("qBittorrent reports the torrent is in an error state", fault="unknown")

        done = progress >= 1.0 and state not in _MOVING
        files = []
        try:
            files = await self._json("torrents/files", hash=info["hash"])
        except DownloadFailure:
            pass
        wanted = [f for f in files if f.get("priority", 1) != 0]
        finished = [Path(f["name"]).name for f in wanted if float(f.get("progress", 0)) >= 1.0]
        if done:
            status = "completed"
        elif state in _QUEUED:
            status = "queued"
        else:
            status = "downloading"
        return DownloadTaskStatus(
            task_id=task_id,
            status=status,
            files_total=len(wanted),
            files_completed=len(finished),
            bytes_total=size,
            bytes_downloaded=int(info.get("downloaded") or size * progress),
            progress_percent=round(progress * 100.0, 1),
            succeeded_filenames=finished if done else [],
            has_active_transfer=status == "downloading" and int(info.get("dlspeed") or 0) > 0,
        )

    async def inspect_materialization(self, handle: TaskHandle) -> DownloadMaterialization:
        info = await self._find(handle)
        if info is None:
            return DownloadMaterialization(state="missing")
        state = info.get("state", "")
        if state in _FAILED_LOCAL | _FAILED_UNKNOWN:
            return DownloadMaterialization(state="failed")
        if float(info.get("progress", 0.0)) < 1.0 or state in _MOVING:
            return DownloadMaterialization(state="active")

        files = await self._json("torrents/files", hash=info["hash"])
        save_path = (info.get("save_path") or "").replace("\\", "/").rstrip("/")
        paths = [
            self._local_path(f"{save_path}/{f['name']}")
            for f in files
            if f.get("priority", 1) != 0
        ]
        present = [str(p) for p in paths if p.is_file()]
        workspace = ""
        if len(files) > 1 and info.get("content_path"):
            workspace = str(self._local_path(info["content_path"]))
        return DownloadMaterialization(
            state="completed",
            workspace_path=workspace,
            file_paths=present,
            mount_healthy=bool(present) or not paths,
        )

    async def _delete(self, handle: TaskHandle, delete_files: bool) -> bool:
        info = await self._find(handle)
        if info is None:
            return False
        response = await self._request(
            "POST",
            "torrents/delete",
            data={"hashes": info["hash"], "deleteFiles": "true" if delete_files else "false"},
        )
        return response.status_code == 200

    async def abort(self, handle: TaskHandle) -> bool:
        return await self._delete(handle, delete_files=True)

    async def discard_client_artifacts(self, handle: TaskHandle) -> bool:
        action = self._setting("discard_action").lower() or "remove"
        if action == "keep":
            return True
        return await self._delete(handle, delete_files=action == "remove_with_files")

    async def diagnose_downloads_mount(self) -> MountDiagnosis:
        return MountDiagnosis(supported=False)
