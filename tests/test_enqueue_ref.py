import json
import logging
from types import SimpleNamespace

from droppedneedle_plugin import EnqueueRequest, Response
from plugin import QBittorrent, encode_ref, torrent_info_hash

from test_plugin import FakeQBit

# d4:infod4:name1:x6:lengthi3eee  -> info dict is the d...e after "4:info"
TORRENT = b"d8:announce3:foo4:infod6:lengthi3e4:name1:xee"
INFO = b"d6:lengthi3e4:name1:xe"


def test_torrent_info_hash():
    import hashlib

    assert torrent_info_hash(TORRENT) == hashlib.sha1(INFO).hexdigest()
    assert torrent_info_hash(b"<html>") == ""


async def test_ref_resolves_to_fresh_torrent_file_and_uploads(tmp_path):
    qbit = FakeQBit()
    release = {"title": "Artist - Album FLAC", "guid": "g1", "indexerId": 4,
               "downloadUrl": "http://prowlarr:9696/4/download?link=fresh", "protocol": "torrent"}
    seen = {}

    async def get(url, **kw):
        seen[url] = kw
        if url.endswith("/api/v1/search"):
            return Response(200, {}, json.dumps([release]).encode(), url)
        return Response(200, {}, TORRENT, url)

    ctx = SimpleNamespace(
        settings={"url": "http://qb:8080", "prowlarr_url": "http://prowlarr:9696", "prowlarr_api_key": "k"},
        http=SimpleNamespace(request=qbit.request, post=lambda url, **kw: qbit.request("POST", url, **kw), get=get),
        logger=logging.getLogger("test"),
        data_dir=tmp_path,
    )
    plugin = QBittorrent(ctx)
    payload = encode_ref(release)
    qbit.torrents.clear()
    # The fake names the new torrent by the hash we tell it to expect.
    original = qbit.request

    async def request(method, url, **kw):
        if url.endswith("torrents/add"):
            kw["data"] = {"_hash": "a" * 40}
        return await original(method, url, **kw)

    ctx.http.request = request
    handle = await plugin.enqueue(EnqueueRequest(task_id="t9", source="plugin:qbittorrent", payload=payload))
    assert handle.job_name == "droppedneedle-t9"
    assert seen["http://prowlarr:9696/4/download?link=fresh"]["headers"] == {"X-Api-Key": "k"}
    add = next(c for c in qbit.calls if c[1] == "torrents/add")[2]
    assert add["category"] == "music" and add["tags"] == "droppedneedle-t9"
