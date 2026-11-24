# DroppedNeedle qBittorrent plugin

A [DroppedNeedle](https://github.com/DroppedNeedle/DroppedNeedle) download client
(`plugin:qbittorrent`) that sends torrents and magnet links to qBittorrent and
**always sets the category to `music`** (configurable; created if missing).

## Install

Paste this repository's URL into **Settings > Plugins**, or copy `plugin.toml`
and `plugin.py` into the server's plugins folder. Enable it and fill in the
settings.

| Setting | Meaning |
|---|---|
| qBittorrent URL | Web UI address, e.g. `http://qbittorrent:8080` |
| Username / Password | Web UI credentials (leave empty if auth is bypassed) |
| Category | Defaults to `music` |
| Downloads directory | Where DroppedNeedle sees qBittorrent's completed files. Finished files must be inside it |
| qBittorrent path | Only if qBittorrent sees that folder under a different path (e.g. `/downloads`) |
| When a download is discarded | `keep`, `remove` (default, torrent only) or `remove_with_files` |

## How it works

- It is a download client only. A torrent indexer plugin supplies the results:
  set its `target_source = "plugin:qbittorrent"` and put a magnet link or
  `.torrent` URL in `PluginSearchResult.payload`.
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
