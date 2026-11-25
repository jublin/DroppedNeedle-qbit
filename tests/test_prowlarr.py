import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from droppedneedle_plugin import Response, ScoringHelper
from plugin import QBittorrent, quality_tier

RELEASES = [
    {"title": "Artist - Album (2020) [FLAC]", "size": 300, "seeders": 10, "protocol": "torrent",
     "magnetUrl": "magnet:?xt=urn:btih:" + "b" * 40},
    {"title": "Artist - Album 2020 MP3 320", "size": 100, "seeders": 50, "protocol": "torrent",
     "downloadUrl": "http://prowlarr:9696/1/download?link=x"},
    {"title": "Artist - Album [FLAC] dead", "size": 1, "seeders": 0, "protocol": "torrent",
     "magnetUrl": "magnet:?xt=urn:btih:" + "c" * 40},
    {"title": "Artist - Album [FLAC] usenet", "size": 1, "seeders": 9, "protocol": "usenet",
     "downloadUrl": "http://x/nzb"},
    {"title": "Other Band - Other", "size": 1, "seeders": 5, "protocol": "torrent",
     "magnetUrl": "magnet:?xt=urn:btih:" + "d" * 40},
]


def make(settings=None, handler=None):
    calls = []

    async def get(url, **kw):
        calls.append((url, kw))
        if handler:
            return await handler(url, kw)
        return Response(200, {}, json.dumps(RELEASES).encode(), url)

    ctx = SimpleNamespace(
        settings={"url": "http://qb:8080", "prowlarr_url": "http://prowlarr:9696", "prowlarr_api_key": "k",
                  **(settings or {})},
        http=SimpleNamespace(get=get),
        logger=logging.getLogger("test"),
        scoring=ScoringHelper(),
    )
    return QBittorrent(ctx), calls


async def test_search_ranks_filters_and_tiers():
    plugin, calls = make()
    results = await plugin.search_album("Artist", "Album", 2020)
    url, kw = calls[0]
    assert url == "http://prowlarr:9696/api/v1/search"
    assert kw["headers"] == {"X-Api-Key": "k"}
    assert kw["params"]["query"] == "Artist Album" and kw["params"]["categories"] == ["3000"]
    found = [r.plugin for r in results]
    # Equal scores are ordered by seeders.
    assert [r.title for r in found] == [
        "Artist - Album 2020 MP3 320",
        "Artist - Album (2020) [FLAC]",
        "Other Band - Other",
    ]
    assert found[0].quality_tier == "mp3_320" and found[1].quality_tier == "lossless"
    assert found[0].payload.startswith("prowlarr:") and found[1].payload.startswith("magnet:")
    assert found[0].score == found[1].score == 1.0 and found[2].score < 0.5
    assert all(r.source == "plugin:qbittorrent" for r in results)


async def test_min_seeders_and_categories_settings():
    plugin, calls = make({"min_seeders": "20", "prowlarr_categories": "3000, 3040"})
    results = await plugin.search_album("Artist", "Album")
    assert [r.plugin.title for r in results] == ["Artist - Album 2020 MP3 320"]
    assert calls[0][1]["params"]["categories"] == ["3000", "3040"]


async def test_unconfigured_returns_nothing():
    plugin, calls = make({"prowlarr_api_key": ""})
    assert await plugin.search_album("Artist", "Album") == []
    assert calls == []


async def test_errors_and_timeouts_return_empty():
    async def boom(url, kw):
        raise OSError("down")

    async def bad(url, kw):
        return Response(401, {}, b"", url)

    async def slow(url, kw):
        await asyncio.sleep(1)

    assert await make(handler=boom)[0].search_album("A", "B") == []
    assert await make(handler=bad)[0].search_album("A", "B") == []
    assert await make(handler=slow)[0].search_album("A", "B", timeout=0.05) == []


async def test_search_track_is_empty():
    assert await make()[0].search_track("A", "T") == []


@pytest.mark.parametrize(
    "title,tier",
    [("A - B [FLAC 24bit]", "lossless"), ("A - B 320kbps 320", "mp3_320"), ("A - B MP3 V0", "mp3_256"),
     ("A - B V2", "mp3_192"), ("A - B 128", "low"), ("A - B", "")],
)
def test_quality_tier(title, tier):
    assert quality_tier(title) == tier


async def test_null_seeders_are_kept():
    async def handler(url, kw):
        rel = [{"title": "Artist - Album FLAC", "seeders": None, "protocol": "torrent", "magnetUrl": "magnet:?xt=urn:btih:" + "e" * 40}]
        return Response(200, {}, json.dumps(rel).encode(), url)

    plugin, _ = make(handler=handler)
    results = await plugin.search_album("Artist", "Album")
    assert [r.plugin.title for r in results] == ["Artist - Album FLAC"]


async def test_cleaned_query_variant_is_searched():
    plugin, calls = make()
    await plugin.search_album("Artist", "Album (Deluxe Edition)")
    assert sorted(kw["params"]["query"] for _, kw in calls) == ["Artist Album", "Artist Album (Deluxe Edition)"]


async def test_retries_without_categories_when_empty():
    async def handler(url, kw):
        found = [] if kw["params"].get("categories") else RELEASES
        return Response(200, {}, json.dumps(found).encode(), url)

    plugin, calls = make(handler=handler)
    results = await plugin.search_album("Artist", "Album")
    assert results and "categories" not in calls[-1][1]["params"]


from plugin import score_release


@pytest.mark.parametrize(
    "artist,album,title,minimum",
    [
        ("Artist", "Album (Deluxe Edition)", "Artist - Album (2020) [FLAC]", 0.95),
        ("Simon & Garfunkel", "Bookends", "Simon and Garfunkel - Bookends 1968 FLAC", 0.95),
        ("The Beatles", "Abbey Road", "Beatles - Abbey Road [24-96 FLAC]", 0.95),
        ("Sigur Rós", "Ágætis byrjun", "Sigur Ros - Agaetis Byrjun (1999) MP3 320", 0.4),
        ("Guns N' Roses", "Appetite for Destruction", "Guns N Roses - Appetite For Destruction FLAC", 0.95),
    ],
)
def test_score_accepts_real_titles(artist, album, title, minimum):
    assert score_release(artist, album, title) >= minimum


def test_score_rejects_wrong_album_and_collections():
    assert score_release("Artist", "Album", "Artist - Other Record FLAC") < 0.7
    assert score_release("Artist", "Album", "Artist - Discography FLAC") < 0.7
    assert score_release("Artist", "Album", "Artist - Album + 5 albums Discography") < 0.7


async def test_payload_is_stable_across_searches():
    n = {"i": 0}

    async def handler(url, kw):
        n["i"] += 1  # Prowlarr issues a fresh download link on every search
        rel = [{"title": "Artist - Album FLAC", "guid": "g1", "indexerId": 4, "seeders": 5,
                "protocol": "torrent", "downloadUrl": f"http://prowlarr/4/download?link=tok{n['i']}"}]
        return Response(200, {}, json.dumps(rel).encode(), url)

    plugin, _ = make(handler=handler)
    first = (await plugin.search_album("Artist", "Album"))[0].plugin.payload
    second = (await plugin.search_album("Artist", "Album"))[0].plugin.payload
    assert first == second and first.startswith("prowlarr:")
