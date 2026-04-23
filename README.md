# browser-migrate

> **Personal, unpolished scripts.** I wrote these to test whether Safari was a viable daily driver by importing my Edge browsing history. They are not a polished tool — no error recovery, no tests, no packaging. Use at your own risk.

Python scripts to migrate Microsoft Edge history to other browsers on macOS. Two targets are supported:

| Target | Script | Notes |
| --- | --- | --- |
| **Safari** | `export_edge.py` | Generates Safari's native `History.json` import format |
| **Chrome-family** | `import_to_chrome.py` | Directly merges into Chrome/Brave/Vivaldi/Chromium SQLite DB |

> **Note:** Chrome's built-in importer (`Settings → Import bookmarks and settings → Microsoft Edge`) handles bookmarks, passwords and autofill but **not** browsing history. `import_to_chrome.py` fills that gap.

## What it does

- Reads Edge's SQLite `History` database from all installed channels (Stable, Dev, Beta, Canary)
- For Safari: converts to Safari's `History.json` schema (Apple Native Format)
- For Chrome-family: merges directly into the target browser's `History` SQLite DB, deduplicating by URL and visit timestamp

## Scripts

| Script | Purpose |
| --- | --- |
| `import_to_chrome.py` | **Edge → Chrome/Brave/Vivaldi/Chromium** direct DB merge |
| `export_edge.py` | Edge history → `History.json` (Safari schema) |
| `validator.py` | Validates a `History.json` against Apple's import schema |
| `final.py` | Alternative Safari export using per-visit rows |
| `extract_edge_all_channels.py` | Scans all installed Edge channels |
| `extract_edge_multiprofile.py` | Multi-profile extraction |
| `extract_full_edge.py` | Full history extraction with channel version metadata |
| `migrate_edge_fixed.py` | Fixed-epoch history migration |
| `migrate_edge_select.py` | Interactive profile selection |

## Requirements

- macOS
- Python 3.12+
- No third-party dependencies — stdlib only

## Usage

### Edge → Chrome / Brave / Vivaldi / Chromium

```bash
python import_to_chrome.py
```

1. Select source Edge profile(s)
2. Select target browser profile
3. **Close the target browser first** — the script will prompt you
4. The script backs up `History` to `History.edge_import.bak` before writing

Detected targets: Google Chrome Dev, Google Chrome Beta, Google Chrome, Google Chrome Canary, Chromium, Brave, Vivaldi.

### Edge → Safari

```bash
python export_edge.py
```

Select a profile or `all` to merge. Output: `History.json` (or `History_<profile>.json`).

Then in Safari: **File → Import From → Browsing Data from File…**

### Validate Safari output

```bash
python validator.py History.json
```

## How the Chrome merge works

Edge and Chrome both use the Chromium SQLite history schema with the same epoch (microseconds since 1601-01-01), so no timestamp conversion is needed.

- **New URLs** are inserted into `urls` with their visit count and timestamp.
- **Existing URLs** get their `visit_count` incremented and `last_visit_time` updated to the later of the two.
- **Visits** are deduplicated by `(url_id, visit_time)` before insertion.

## Notes

- Edge's `History` DB is locked while Edge is running. The scripts copy it to `/tmp` before reading.
- Exported JSON/HTML data files are gitignored — they contain personal browsing data.
