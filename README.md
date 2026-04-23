# browser-migrate

> **Personal, unpolished scripts.** I wrote these to test whether Safari was a viable daily driver by importing my Edge browsing history. They are not a polished tool — no error recovery, no tests, no packaging. Use at your own risk.

Python scripts to migrate browser data on macOS between Chromium-based browsers (Edge, Chrome, Brave, Vivaldi, Chromium) and to Safari.

## Scripts

| Script | Purpose |
| --- | --- |
| `migrate.py` | **Main tool** — bidirectional merge between any Chromium browsers |
| `export_edge.py` | Edge history → `History.json` (Safari import format) |
| `validator.py` | Validates a `History.json` against Apple's import schema |
| `final.py` | Alternative Safari export using per-visit rows |
| `extract_edge_all_channels.py` | Scans all installed Edge channels |
| `extract_edge_multiprofile.py` | Multi-profile extraction |
| `extract_full_edge.py` | Full history extraction with channel version metadata |
| `migrate_edge_fixed.py` | Fixed-epoch history migration |
| `migrate_edge_select.py` | Interactive profile selection |

## migrate.py — Chromium ↔ Chromium

Works in any direction: Edge → Chrome, Chrome → Edge, Chrome → Brave, etc.

**What it migrates (all with deduplication):**

| Data | Stored as | Deduplicated by |
| --- | --- | --- |
| History | SQLite `History` DB | URL + visit timestamp |
| Bookmarks | JSON `Bookmarks` file | URL (folder structure preserved) |
| Autofill form values | SQLite `Web Data` | (field name, value) pair |

**Not migrated** — encrypted with browser-specific OS keychain keys:

- Saved passwords (`Login Data`)
- Credit cards (`Web Data`)

### Usage

```bash
uv run python migrate.py
```

1. Pick source profile(s) — any installed browser, or `all`
2. Pick target profile — single profile, source excluded
3. **Close the target browser** — the script will prompt you
4. Each modified file is backed up to `<file>.migrator.bak` first

Detected browsers: Microsoft Edge (Stable/Dev/Beta/Canary), Google Chrome (Stable/Dev/Beta/Canary), Chromium, Brave, Vivaldi.

### How deduplication works

All browsers in this list use the Chromium SQLite schema and the same epoch (microseconds since 1601-01-01), so no timestamp conversion is needed.

- **History URLs:** if already present, `visit_count` is incremented and `last_visit_time` updated to the later value.
- **History visits:** deduplicated by `(url_id, visit_time)`.
- **Bookmarks:** URL already present anywhere in the target tree → skipped. Folders are matched by name and merged recursively; new folders are only created if they contain at least one new URL.
- **Autofill:** if `(name, value)` already exists, usage `count` is incremented.

## export_edge.py — Edge → Safari

Chrome's built-in importer handles bookmarks but not history. This script fills the history gap for Safari.

```bash
uv run python export_edge.py
```

Select a profile or `all` to merge across profiles. Output: `History.json`.

Then in Safari: **File → Import From → Browsing Data from File…**

```bash
uv run python validator.py History.json   # validate before importing
```

## Requirements

- macOS
- Python 3.12+
- No third-party dependencies — stdlib only

## Notes

- Source `History` DB is locked while the browser is running — scripts copy it to `/tmp` before reading.
- Exported JSON/HTML data files are gitignored — they contain personal browsing data.
