# DroppedNeedle Prowlarr + qBittorrent plugin

A [DroppedNeedle](https://github.com/DroppedNeedle/DroppedNeedle) plugin
(`plugin:qbittorrent`) that:

1. **Searches** torrent indexers through Prowlarr (`indexer` capability).
2. **Sends** the chosen torrent or magnet link to qBittorrent, **always setting
   the category to `music`** (configurable; created if missing).
3. **Monitors** progress and completion, then hands the finished files to
   DroppedNeedle's normal import pipeline (`download_client` capability).

Results carry a score and a quality tier (`lossless`, `mp3_320`, ...) inferred
from the release title, so DroppedNeedle's own quality settings, score
thresholds and source priority decide what downloads automatically.

## Install

Paste this repository's URL into **Settings > Plugins**, or copy `plugin.toml`
and `plugin.py` into the server's plugins folder. Enable it and fill in the
settings.

| Setting | Meaning |
|---|---|
| Prowlarr URL / API key | Prowlarr address and key (Settings > General). Leave empty to use a different indexer plugin |
| Prowlarr categories | Newznab ids, default `3000` (Audio) |
| Minimum seeders | Default `1` |
| qBittorrent URL | Web UI address, e.g. `http://qbittorrent:8080` |
| API key | qBittorrent API key (Options > WebUI), sent as a Bearer token; no login needed. Overrides username/password |
| Username / Password | Web UI credentials (leave empty if auth is bypassed) |
| Category | Defaults to `music` |
| Downloads directory | Where DroppedNeedle sees qBittorrent's completed files. Finished files must be inside it |
| qBittorrent path | Only if qBittorrent sees that folder under a different path (e.g. `/downloads`) |
| When a download is discarded | `keep`, `remove` (default, torrent only) or `remove_with_files` |

## How it works

- Only torrent results (not usenet) are returned; the payload is the magnet
  link, or Prowlarr's `.torrent` download URL when there is none. In that case
  qBittorrent must be able to reach the Prowlarr address you configured.
- Another indexer plugin can still feed this client: set its
  `target_source = "plugin:qbittorrent"` and put a magnet link or `.torrent`
  URL in the result payload.
- Track-level search is not supported (torrents are albums).
- Each torrent gets a `droppedneedle-<task id>` tag, which is how later status
  calls find it. The plugin keeps no state.
- Folder mode: when the torrent finishes, the files qBittorrent reports are
  translated through the path mapping and handed to DroppedNeedle.
- Aborted downloads are deleted together with their partial files.

## Development

```bash
uv sync
uv run pytest
```

`vendor/droppedneedle_plugin.py` is a copy of the DroppedNeedle SDK for tests and
editor completion; DroppedNeedle provides the real one at runtime.

## Troubleshooting "no sources have this release"

The server log (tagged `plugin=qbittorrent`) shows, per search, the queries
sent to Prowlarr, how many raw results came back, and why results were
skipped (usenet, no link, too few seeders). Check that:

- the plugin card is healthy and `plugin:qbittorrent` is in the source priority list;
- Prowlarr answers in time (the host allows 35 s; disable slow indexers in Prowlarr);
- the same query returns results in Prowlarr's own search page.

Releases with no seeder count are kept; lower *Minimum seeders* to `0` for indexers that report zero.
