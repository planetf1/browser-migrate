# browser-migrate

> **Personal, unpolished scripts.** Written to test whether Safari was a viable daily driver by migrating Edge browsing data. Not a polished tool — no error recovery, no tests, no packaging. Use at your own risk and keep backups.

Migrate browsing data on macOS between Chromium-based browsers (Edge, Chrome, Brave, Vivaldi, Chromium) and to Safari.

## What gets migrated

### Chromium ↔ Chromium (`migrate.py`)

| Data | How stored | Deduplicated by |
| --- | --- | --- |
| Browsing history | SQLite `History` DB | URL + visit timestamp |
| Bookmarks | JSON `Bookmarks` file | URL — folder structure preserved |
| Autofill form values | SQLite `Web Data` | (field name, value) pair |

**Not migrated** — encrypted with browser-specific OS keychain keys, no practical way to move them:

- Saved passwords (`Login Data`)
- Saved credit cards (`Web Data`)

### Edge → Safari (`export_edge.py`)

Browsing history only, exported as Safari's native `History.json` import format.

> Chrome's built-in importer (`Settings → Import bookmarks and settings → Microsoft Edge`) handles bookmarks, passwords, and autofill — but **not** history. `export_edge.py` fills that gap for Safari.

---

## Requirements

- macOS (reads from `~/Library/Application Support/`)
- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) (or plain `python3` — no third-party dependencies)

```bash
git clone https://github.com/planetf1/browser-migrate
cd browser-migrate
uv sync   # or skip — there are no dependencies to install
```

---

## Usage: Chromium ↔ Chromium

```bash
uv run python migrate.py
```

**Step 1 — pick a source.** Any installed browser and profile. Type a number or `all` to merge every profile.

```text
--- Chromium Browser Data Migrator ---
Migrates: History · Bookmarks · Autofill form values

Source profiles:
  [1] Microsoft Edge Canary - Work Profile [Default] (work@company.com)
  [2] Microsoft Edge Canary - Personal [Profile 1] (me@example.com)
  [3] Google Chrome Dev - Your Chrome [Default] (me@gmail.com)
  [4] Google Chrome Beta - Your Chrome [Default] (me@gmail.com)

Select number, or  all: 1
```

**Step 2 — pick a target.** The source profile is excluded from the list automatically.

```text
Target profiles:
  [1] Microsoft Edge Dev - Work Profile [Default] (work@company.com)
  [2] Microsoft Edge Dev - Personal [Profile 1] (me@example.com)
  [3] Google Chrome Dev - Your Chrome [Default] (me@gmail.com)
  [4] Google Chrome Dev - Your Chrome [Profile 1] (work@gmail.com)
  [5] Google Chrome Beta - Your Chrome [Default] (me@gmail.com)

Select number: 4
```

**Step 3 — close the target browser** when prompted, then press Enter.

```text
⚠  Make sure Google Chrome Dev - Your Chrome [Profile 1] is fully closed.
Press Enter when ready...
```

**Step 4 — done.**

```text
Reading source history...
     2,758 URLs       8,140 visits  ← Microsoft Edge Canary - Work Profile

── Google Chrome Dev - Your Chrome [Profile 1] ──
  Backed up → History.migrator.bak
  History:   +2,758 URLs  +8,126 visits
  Backed up → Bookmarks.migrator.bak
  Bookmarks: +161
  Backed up → Web Data.migrator.bak
  Autofill:  +68 form values

Done. Launch the target browser to verify.
```

### Backups

Before touching any file, the script writes `<filename>.migrator.bak` alongside the original in the browser's profile directory. If anything goes wrong, copy it back and restart the browser.

```text
~/Library/Application Support/Google/Chrome Dev/Default/
  History
  History.migrator.bak       ← safe to delete once you've verified
  Bookmarks
  Bookmarks.migrator.bak
  Web Data
  Web Data.migrator.bak
```

### Supported browsers

Auto-detected from `~/Library/Application Support/`:

| Browser | Channels |
| --- | --- |
| Microsoft Edge | Stable, Dev, Beta, Canary |
| Google Chrome | Stable, Dev, Beta, Canary |
| Chromium | — |
| Brave | — |
| Vivaldi | — |

Multiple profiles per browser are all detected and listed.

---

## Usage: Edge → Safari

```bash
uv run python export_edge.py
```

```text
--- Edge → Safari History (Apple schema) ---

Found Profiles:
[1] Microsoft Edge Canary - Work Profile (work@company.com)
[2] Microsoft Edge Canary - Personal (me@example.com)

Select profile number (or type 'all'): all

Merging 2 profiles...
   + 2,758 URLs from Microsoft Edge Canary - Work Profile
   + 452 URLs from Microsoft Edge Canary - Personal
✅ Created: History.json (2,891 items)

--- Next Step ---
Open Safari → File → Import From → Browsing Data from File…
Choose the History.json file you just created.
```

Validate the output before importing:

```bash
uv run python validator.py History.json
# ✅ Schema looks OK — 2,891 item(s).
```

Then in Safari: **File → Import From → Browsing Data from File…** and select `History.json`.

---

## How it works

### Why no timestamp conversion for Chromium browsers?

All Chromium-derived browsers (Edge, Chrome, Brave, Vivaldi, Chromium) store timestamps as microseconds since 1601-01-01. Because they share the same epoch and schema, history rows can be inserted directly with no conversion.

Safari uses Unix microseconds (since 1970-01-01), which is why `export_edge.py` subtracts `11644473600 × 10⁶` before writing.

### Bookmark merge strategy

Folders are matched by name and merged recursively. A new folder is only created in the target if it would contain at least one URL not already present. Empty folders are never created.

```text
Edge "bookmark_bar"              Chrome "bookmark_bar" (after merge)
├── Work/                        ├── UK/                  ← existing, untouched
│   ├── Jira                     ├── Work/                ← new folder created
│   └── Confluence               │   ├── Jira             ← new bookmark
│                                │   └── Confluence       ← new bookmark
└── github.com    ← already      └── github.com           ← skipped (duplicate)
    in Chrome
```

### Source DB locking

A browser's `History` and `Web Data` SQLite files are locked while the browser is running. The scripts copy them to `/tmp` before reading, so the source browser can stay open. **Only the target must be closed** — the script writes directly to the target's files.

---

## Scripts

| Script | Purpose |
| --- | --- |
| `migrate.py` | **Main tool** — bidirectional Chromium ↔ Chromium migration |
| `export_edge.py` | Edge history → `History.json` (Safari schema) |
| `validator.py` | Validates a `History.json` against Apple's import schema |
| `final.py` | Alternative Safari export using per-visit rows |
| `extract_edge_all_channels.py` | Scans all installed Edge channels |
| `extract_edge_multiprofile.py` | Multi-profile extraction |
| `extract_full_edge.py` | Full history extraction with channel version metadata |
| `migrate_edge_fixed.py` | Fixed-epoch history migration |
| `migrate_edge_select.py` | Interactive profile selection |
