"""qBittorrent download client for DroppedNeedle.

Sends the torrent or magnet link an indexer supplies (``EnqueueRequest.payload``)
to qBittorrent's Web API v2, always setting the configured category (default
``music``). Torrents are identified by a per-task tag, so the plugin keeps no
state and survives restarts. Folder mode: the finished files are discovered
from qBittorrent when the torrent completes.
"""

import asyncio
import base64
import re
from pathlib import Path
from urllib.parse import urlsplit

from droppedneedle_plugin import (
    DownloadFailure,
    DownloadMaterialization,
    DownloadTaskStatus,
    EnqueueRequest,
    MountDiagnosis,
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


def _text(value) -> str:
    return (value or "").strip() if isinstance(value, str) else ""


class QBittorrent:
    def __init__(self, ctx):
        self.ctx = ctx
        self._sid: str | None = None
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

    # -- HTTP -----------------------------------------------------------

    async def _login(self) -> None:
        base = self._base_url()
        if not base:
            raise DownloadFailure("qBittorrent URL is not configured", fault="local")
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
            raise DownloadFailure("qBittorrent login failed: check the username and password", fault="local")
        self._sid = None
        for key, value in response.headers.items():
            if key.lower() == "set-cookie":
                match = re.search(r"SID=([^;]+)", value)
                if match:
                    self._sid = match.group(1)
        # No cookie with an "Ok." answer means authentication is bypassed.

    async def _request(self, method: str, path: str, *, params=None, data=None, timeout: float = 30.0):
        base = self._base_url()
        if not base:
            raise DownloadFailure("qBittorrent URL is not configured", fault="local")
        async with self._login_lock:
            if self._sid is None:
                await self._login()
        for attempt in (1, 2):
            headers = {"Referer": base}
            if self._sid:
                headers["Cookie"] = f"SID={self._sid}"
            try:
                response = await self.ctx.http.request(
                    method, f"{base}/api/v2/{path}", params=params, data=data, headers=headers, timeout=timeout
                )
            except OSError as exc:
                raise DownloadFailure(f"cannot reach qBittorrent at {base}: {exc}", fault="local") from exc
            if response.status_code == 403 and attempt == 1:
                async with self._login_lock:
                    await self._login()
                continue
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
        return ServiceStatus(status="ok", version=response.text.strip())

    # -- download client ------------------------------------------------

    async def enqueue(self, request: EnqueueRequest) -> TaskHandle:
        payload = (request.payload or "").strip()
        if not payload or not (payload.lower().startswith("magnet:") or urlsplit(payload).scheme in ("http", "https")):
            raise DownloadFailure(
                "qBittorrent needs a magnet link or .torrent URL in the result payload", fault="source"
            )
        category = self._category()
        tag = self._tag(request.task_id)
        await self._ensure_category(category)

        response = await self._request(
            "POST", "torrents/add", data={"urls": payload, "category": category, "tags": tag}, timeout=60.0
        )
        body = response.text.strip()
        if response.status_code == 415:
            raise DownloadFailure("qBittorrent rejected the torrent file", fault="source")
        if response.status_code != 200:
            raise DownloadFailure(f"qBittorrent answered HTTP {response.status_code} to add", fault="unknown")
        if body.lower().startswith("fails"):
            # Most likely already in qBittorrent: adopt it instead of failing.
            info_hash = self._magnet_hash(payload)
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
