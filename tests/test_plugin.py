import logging
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from droppedneedle_plugin import DownloadFailure, EnqueueRequest, Response, TaskHandle
from plugin import QBittorrent

HASH = "a" * 40
MAGNET = f"magnet:?xt=urn:btih:{HASH}&dn=Artist+-+Album"


class FakeQBit:
    """Just enough of the qBittorrent Web API v2."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.categories: dict = {}
        self.torrents: dict[str, dict] = {}
        self.files: dict[str, list] = {}
        self.login_ok = True
        self.add_result = "Ok."

    async def request(self, method, url, *, params=None, data=None, headers=None, timeout=30.0):
        path = url.split("/api/v2/", 1)[1]
        form = {k: v for k, v in (data or {}).items()}
        self.calls.append((method, path, {**(params or {}), **form, "_headers": headers or {}}))
        if path == "auth/login":
            if not self.login_ok:
                return Response(200, {}, b"Fails.", url)
            return Response(200, {"Set-Cookie": "SID=abc123; HttpOnly; path=/"}, b"Ok.", url)
        if headers.get("Cookie") != "SID=abc123":
            return Response(403, {}, b"Forbidden", url)
        if path == "app/version":
            return Response(200, {}, b"v5.0.1", url)
        if path == "torrents/categories":
            return self._json(self.categories, url)
        if path == "torrents/createCategory":
            self.categories[form["category"]] = {"name": form["category"], "savePath": ""}
            return Response(200, {}, b"", url)
        if path == "torrents/add":
            if self.add_result == "Ok.":
                self.torrents[HASH] = {
                    "hash": HASH,
                    "state": "downloading",
                    "progress": 0.5,
                    "size": 100,
                    "save_path": "/downloads/music",
                    "content_path": "/downloads/music/Album",
                    "category": form["category"],
                    "tags": form["tags"],
                }
            return Response(200, {}, self.add_result.encode(), url)
        if path == "torrents/info":
            if "tag" in params:
                found = [t for t in self.torrents.values() if params["tag"] in t["tags"].split(", ")]
            else:
                found = [t for t in self.torrents.values() if t["hash"] in params["hashes"].split("|")]
            return self._json(found, url)
        if path == "torrents/files":
            return self._json(self.files.get(params["hash"], []), url)
        if path == "torrents/addTags":
            self.torrents[form["hashes"]]["tags"] = form["tags"]
            return Response(200, {}, b"", url)
        if path == "torrents/setCategory":
            self.torrents[form["hashes"]]["category"] = form["category"]
            return Response(200, {}, b"", url)
        if path == "torrents/delete":
            self.torrents.pop(form["hashes"], None)
            return Response(200, {}, b"", url)
        return Response(404, {}, b"", url)

    @staticmethod
    def _json(value, url):
        import json

        return Response(200, {}, json.dumps(value).encode(), url)


@pytest.fixture
def qbit():
    return FakeQBit()


@pytest.fixture
def plugin(qbit, tmp_path):
    ctx = SimpleNamespace(
        settings={"url": "http://qb:8080", "username": "u", "password": "p", "downloads_dir": str(tmp_path)},
        http=SimpleNamespace(request=qbit.request, post=lambda url, **kw: qbit.request("POST", url, **kw)),
        logger=logging.getLogger("test"),
        data_dir=tmp_path,
    )
    return QBittorrent(ctx)


def _request(payload=MAGNET, task_id="t1"):
    return EnqueueRequest(task_id=task_id, source="plugin:qbittorrent", payload=payload)


async def test_enqueue_sets_music_category_and_tag(plugin, qbit):
    handle = await plugin.enqueue(_request())
    add = next(c for c in qbit.calls if c[1] == "torrents/add")[2]
    assert add["category"] == "music"
    assert add["urls"] == MAGNET
    assert add["tags"] == "droppedneedle-t1"
    assert "music" in qbit.categories
    assert handle.job_name == "droppedneedle-t1"
    assert handle.source == "plugin:qbittorrent"


async def test_custom_category(plugin, qbit):
    plugin.ctx.settings["category"] = "lossless"
    await plugin.enqueue(_request())
    assert qbit.torrents[HASH]["category"] == "lossless"


async def test_login_cookie_reused_and_referer_sent(plugin, qbit):
    await plugin.enqueue(_request())
    assert sum(1 for c in qbit.calls if c[1] == "auth/login") == 1
    assert all(c[2]["_headers"].get("Referer") == "http://qb:8080" for c in qbit.calls)


async def test_bad_credentials_is_local_failure(plugin, qbit):
    qbit.login_ok = False
    with pytest.raises(DownloadFailure) as err:
        await plugin.enqueue(_request())
    assert err.value.fault == "local"


async def test_rejects_payload_without_link(plugin):
    with pytest.raises(DownloadFailure) as err:
        await plugin.enqueue(_request(payload="/some/path"))
    assert err.value.fault == "source"


async def test_existing_torrent_is_adopted(plugin, qbit):
    qbit.torrents[HASH] = {"hash": HASH, "state": "uploading", "progress": 1.0, "tags": "", "category": ""}
    qbit.add_result = "Fails."
    handle = await plugin.enqueue(_request())
    assert qbit.torrents[HASH]["category"] == "music"
    assert (await plugin._find(handle))["hash"] == HASH


async def test_failed_add_of_unknown_torrent_blames_source(plugin, qbit):
    qbit.add_result = "Fails."
    with pytest.raises(DownloadFailure) as err:
        await plugin.enqueue(_request(payload="https://x.test/a.torrent"))
    assert err.value.fault == "source"


async def test_status_downloading_then_completed(plugin, qbit):
    handle = await plugin.enqueue(_request())
    qbit.files[HASH] = [
        {"name": "Album/01.flac", "progress": 1.0, "priority": 1},
        {"name": "Album/02.flac", "progress": 0.0, "priority": 1},
    ]
    status = await plugin.get_status(handle)
    assert (status.status, status.progress_percent, status.files_total, status.files_completed) == (
        "downloading",
        50.0,
        2,
        1,
    )
    qbit.torrents[HASH].update(state="stalledUP", progress=1.0)
    qbit.files[HASH][1]["progress"] = 1.0
    status = await plugin.get_status(handle)
    assert status.status == "completed"
    assert status.succeeded_filenames == ["01.flac", "02.flac"]


async def test_status_missing_files_is_local_fault(plugin, qbit):
    handle = await plugin.enqueue(_request())
    qbit.torrents[HASH]["state"] = "missingFiles"
    with pytest.raises(DownloadFailure) as err:
        await plugin.get_status(handle)
    assert err.value.fault == "local"


async def test_status_unknown_torrent_fails(plugin):
    status = await plugin.get_status(TaskHandle(source="plugin:qbittorrent", job_name="droppedneedle-gone"))
    assert status.status == "failed"


async def test_materialization_maps_remote_path(plugin, qbit, tmp_path):
    plugin.ctx.settings["remote_path"] = "/downloads"
    handle = await plugin.enqueue(_request())
    album = tmp_path / "music" / "Album"
    album.mkdir(parents=True)
    (album / "01.flac").write_bytes(b"x")
    (album / "02.flac").write_bytes(b"x")
    qbit.files[HASH] = [
        {"name": "Album/01.flac", "progress": 1.0, "priority": 1},
        {"name": "Album/02.flac", "progress": 1.0, "priority": 1},
    ]
    assert (await plugin.inspect_materialization(handle)).state == "active"
    qbit.torrents[HASH].update(state="stalledUP", progress=1.0)
    result = await plugin.inspect_materialization(handle)
    assert result.state == "completed"
    assert result.file_paths == [str(album / "01.flac"), str(album / "02.flac")]
    assert result.workspace_path == str(album)
    assert result.mount_healthy


async def test_materialization_unhealthy_mount(plugin, qbit):
    handle = await plugin.enqueue(_request())
    qbit.files[HASH] = [{"name": "Album/01.flac", "progress": 1.0, "priority": 1}]
    qbit.torrents[HASH].update(state="stalledUP", progress=1.0)
    result = await plugin.inspect_materialization(handle)
    assert result.file_paths == [] and not result.mount_healthy


async def test_discard_actions(plugin, qbit):
    handle = await plugin.enqueue(_request())
    plugin.ctx.settings["discard_action"] = "keep"
    assert await plugin.discard_client_artifacts(handle)
    assert HASH in qbit.torrents
    plugin.ctx.settings["discard_action"] = "remove_with_files"
    assert await plugin.discard_client_artifacts(handle)
    delete = next(c for c in qbit.calls if c[1] == "torrents/delete")[2]
    assert delete["deleteFiles"] == "true" and HASH not in qbit.torrents


async def test_abort_deletes_files(plugin, qbit):
    handle = await plugin.enqueue(_request())
    assert await plugin.abort(handle)
    assert next(c for c in qbit.calls if c[1] == "torrents/delete")[2]["deleteFiles"] == "true"
    assert not await plugin.abort(handle)


async def test_health(plugin, qbit, tmp_path):
    status = await plugin.health_check()
    assert (status.status, status.version) == ("ok", "v5.0.1")
    plugin.ctx.settings["downloads_dir"] = str(tmp_path / "nope")
    assert (await plugin.health_check()).status == "error"
    plugin.ctx.settings["url"] = ""
    assert not plugin.is_configured()


def test_magnet_hash_base32():
    import base64

    raw = bytes(range(20))
    b32 = base64.b32encode(raw).decode()
    assert QBittorrent._magnet_hash(f"magnet:?xt=urn:btih:{b32}") == raw.hex()
