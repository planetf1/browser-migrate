# browser-migrate

Python scripts to export Microsoft Edge history and bookmarks to **Safari's** native import format on macOS.

> **Note:** This tooling targets Safari, not Chrome/Chromium. If you want to migrate Edge → Chrome/Chrome Dev, use Chrome's built-in importer: `Settings → Import bookmarks and settings → Microsoft Edge`.

## What it does

- Reads Edge's SQLite `History` database and bookmark HTML files from all installed channels (Stable, Dev, Beta, Canary)
- Converts history to Safari's `History.json` schema (Apple Native Format)
- Supports multi-profile extraction and cross-profile deduplication/aggregation
- Validates the output against Apple's import schema before use

## Scripts

| Script | Purpose |
|---|---|
| `export_edge.py` | Main export: Edge history → `History.json` (Safari schema) |
| `final.py` | Alternative export with per-visit rows instead of per-URL aggregation |
| `extract_edge_all_channels.py` | Scans all installed Edge channels |
| `extract_edge_multiprofile.py` | Multi-profile extraction |
| `extract_full_edge.py` | Full history extraction with channel version metadata |
| `migrate_edge_fixed.py` | Fixed-epoch history migration |
| `migrate_edge_select.py` | Interactive profile selection |
| `validator.py` | Validates a `History.json` file against Apple's schema |

## Requirements

- macOS (reads from `~/Library/Application Support/Microsoft Edge*`)
- Python 3.12+
- No third-party dependencies — stdlib only

## Usage

### Export history

```bash
python export_edge.py
```

Select a profile number or type `all` to merge all profiles. Output: `History.json` (or `History_<profile>.json` for single-profile exports).

### Validate output

```bash
python validator.py History.json
```

### Import into Safari

1. Open Safari
2. **File → Import From → Browsing Data from File…**
3. Select the generated `History.json`

## Schema

Safari's importable history JSON format:

```json
{
  "metadata": {
    "browser_name": "Microsoft Edge Canary",
    "browser_version": "131.0.0.0",
    "data_type": "history",
    "export_time_usec": 1734432000000000,
    "schema_version": 1
  },
  "history": [
    {
      "url": "https://example.com",
      "title": "Example Domain",
      "time_usec": 1734000000000000,
      "visits_count": 3
    }
  ]
}
```

`time_usec` is Unix time in microseconds. Edge stores timestamps as microseconds since 1601-01-01; the scripts subtract `11644473600 × 10⁶` to convert.

## Notes

- Edge's `History` SQLite DB is locked while Edge is running. The scripts copy it to a temp file before reading.
- Exported JSON/HTML data files are gitignored — they contain personal browsing data.
