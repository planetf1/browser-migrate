# browser-migrate

> **Personal, unpolished scripts.** Written to test whether Safari was a viable daily driver by migrating Edge browsing data. Not a polished tool — no error recovery, no tests, no packaging. Use at your own risk and keep backups.

Migrate browsing data on macOS between Chromium-based browsers (Edge, Chrome, Brave, Vivaldi, Chromium) and Safari, in any direction, via one interactive tool (`migrate.py`).

## What gets migrated

### Any direction (`migrate.py`)

| Data | Chromium | Safari | Deduplicated by |
| --- | --- | --- | --- |
| Browsing history | SQLite `History` DB | per-profile `History.db` | URL + visit timestamp (±2 µs Safari round-trip tolerance) |
| Bookmarks | JSON `Bookmarks` file | shared `Bookmarks.plist` | URL — folder structure preserved |
| Autofill form values | SQLite `Web Data` | not supported | (field name, value) pair |

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

## Usage: `migrate.py` (the one command)

```bash
uv run migrate.py            # dry run: plan only, nothing written
uv run migrate.py --apply    # write (backups first, browsers must be quit)
```

One interactive flow for Chromium ↔ Chromium, Chromium → Safari and
Safari → Chromium. History, bookmarks, and autofill (Chromium targets
only; Safari has no accessible autofill store).

**Step 1 — classify roles.** Every discovered profile (Chromium and
Safari) is listed once; you say which are work, everything else is
personal. Profiles sharing an account email inherit the same role.
Nothing is hardcoded: no UUIDs, no emails in the source.

```text
── Profiles ────────────────────────────────────────
  [1] Microsoft Edge Beta — Profile 1 [Default] (nigel@cherrybyte.me.uk)
  [2] Microsoft Edge Beta — Profile 2 [Profile 1] (jonesn@uk.ibm.com)
  …
  [8] Safari — 1B5279A1

── Roles ───────────────────────────────────────────
Which profiles are WORK? Everything else is personal.
Work profiles (numbers, or Enter for none): 2 8
  work      Microsoft Edge Beta — Profile 2 [Profile 1] (jonesn@uk.ibm.com), Safari — 1B5279A1
  personal  Microsoft Edge Beta — Profile 1 [Default] (nigel@cherrybyte.me.uk), …
```

**Step 2 — pick a source.** Any installed browser and profile; numbers
or `all`.

**Step 3 — pick a target.** Individual profiles, or a per-browser group
entry (`Safari — all profiles (routed by role)`) that routes each source
to the same-role target profile in one run.

**Routing insists work → work, personal → personal.** Cross-role
selections are refused with an explanation; `--allow-cross-role`
overrides (history only; bookmark zones never cross).

**Dry run by default.** The plan shows exactly what `--apply` would do,
computed against read-only snapshots. Sources may stay open. For
`--apply` the tool itself checks every target browser is quit (Safari via
`pgrep -x Safari`, Chromium via the full `.app` bundle path, so Teams'
embedded Edge helpers never false-positive) and refuses to run otherwise.

```text
── History ─────────────────────────────────────────

  → Safari — default (~/Library/Safari)
  History:  +651 URLs  (9,956 already present)  (dry run)

  → Safari — 1B5279A1
  History:  +718 URLs  (4,841 already present)  (dry run)

── Bookmarks ───────────────────────────────────────

  Chromium → Safari shared plist (curated filing)
  …

── Dry run ─────────────────────────────────────────
Nothing written. Re-run with --apply to make these changes.
```

Per-direction semantics (all established by the October 2026 migration):

- **Chromium → Safari history**: direct `History.db` write, one visit per
  URL (latest), stamped `current_generation + 1` so rows reach iCloud
  history sync. After `--apply` the tool offers to watch the upload
  watermark (`last_synced_generation` landing exactly on the stamp is the
  proof the rows went up).
- **Safari → Chromium history**: per-visit rows with `(url, visit_time
  ± 2 µs)` dedupe (the Cocoa REAL round-trip drifts 1–2 µs).
- **Chromium → Safari bookmarks**: curated filing into the shared
  `Bookmarks.plist` (personal → scheme folders, work → `Work/` subtree,
  Edge Workspaces → `Edge Workspaces/`), junk-filtered, idempotent.
- **Safari → Chromium bookmarks**: insert-only under a reversible
  `From Safari` folder in the target's `other` root; the `Work/` subtree
  only ever targets a work profile.
- **Autofill**: Chromium ↔ Chromium only.

### Backups

Before touching any file, every writer takes a timestamped backup
(`<filename>.migrator-<YYYYMMDD-HHMMSS>.bak`, including SQLite
`-wal`/`-shm` sidecars; Safari `History.db` uses a consistent `VACUUM INTO`
backup). If anything goes wrong, copy it back and restart the browser.

```text
~/Library/Application Support/Google/Chrome Dev/Default/
  History
  History.migrator-20261028-174102.bak   ← safe to delete once you've verified
  Bookmarks.migrator-20261028-174102.bak
  Web Data.migrator-20261028-174102.bak
```

### Supported browsers

Auto-detected from `~/Library/Application Support/` (Chromium family)
plus Safari (default profile and every container profile with a
`History.db`):

| Browser | Channels |
| --- | --- |
| Microsoft Edge | Stable, Dev, Beta, Canary |
| Google Chrome | Stable, Dev, Beta, Canary |
| Chromium | — |
| Brave | — |
| Vivaldi | — |
| Safari | default + per-profile containers |

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

> ⚠️ Safari's built-in importer targets the default profile only, and on macOS 27
> can mis-target a phantom profile record after a profile delete/recreate
> (WBSHistoryErrorDomain Code=3). For per-profile imports (work/personal), use
> the direct path below.

---

## Usage: Edge → Safari, per profile (direct DB import)

`migrate.py` runs this path directly (Chromium source → Safari target,
no intermediate JSON). The standalone CLI remains for importing an
existing `History.json` export; it maps Edge profiles onto Safari profiles
(Edge `Default` → Safari `default`, Edge `Profile 1` → the Safari work
profile UUID) and writes history straight into each profile's `History.db`.
Safari must be quit; every run takes a timestamped `VACUUM INTO` backup first.

```bash
uv run direct_import_safari.py History_<edge-profile>.json <uuid-prefix>
```

Rows are stamped `current_generation + 1` so the next Safari launch leaves
them inside the sync upload window (see below) and the first idle cycle
pushes them to iCloud.

If rows were imported with an older stamping scheme (at the watermark or at
`current`), recover them with:

```bash
uv run restamp_imports.py <uuid-prefix> --range 1:4086 --range 4201:999999 \
    --export History_edge_work.json --apply
```

`--range` bounds are visit-id ranges of the import batches (max visit id in
the pre-import backup : max id in the post-import backup); rows are only
re-stamped when their `(url, visit_time)` matches the Edge exports, so
native Safari rows are never touched. `push_history_sync.py` is the older,
less conservative variant (bump by generation selector) — same `current+1`
rule, no export matching.

### Bookmarks

Safari shares one `Bookmarks.plist` across profiles; the September 2026
migration used:

- `sync_edge_bookmarks.py` — delta-sync Edge bookmarks into the shared tree
  (personal URLs classified into scheme folders via `categorise_bookmarks.py`,
  work URLs preserved under a `Work/` subtree, Edge Workspaces under
  `Edge Workspaces/`), junk-filtered, dry-run first.
- `dedupe_bookmarks.py` — repairs Reading List strays (leaf titles matching
  `[+N]` at root/in-folder), dedupes Reading List and bookmarks by URL with
  folder-priority winner selection, strips Apple seeded default favourites
  from the bar and cluster folders, prunes emptied folders. Dry-run first;
  refuses to relocate more than 25 % of leaves (guards a broken stray
  detector).
- `reorganise_bookmarks.py` / `categorise_bookmarks.py` — the original
  September reorganisation and classification ruleset.

Plist edits are local-only (no CloudKit change records). To propagate
bookmark changes to devices, delete/re-add the affected folders in the
Safari UI so the deletions sync, or use the export → delete-all → re-import
HTML reconciliation described in the `macos-safari-migration` skill.

---

## Usage: Safari → Edge (reverse pull)

`migrate.py` covers this in one command (pick Safari profiles as source,
the Edge Beta group as target). The standalone scripts remain for
date-filtered pulls and account-email verification against a fixed profile
map (personal → `Default`, work → `Profile 1`; the quit-guard matches the
Edge **Beta** bundle path — Teams' embedded Edge WebView2 helpers
false-positive a naive name check):

```bash
uv run safari_to_edge.py --since 2026-10-08 --apply        # history
uv run safari_to_edge_bookmarks.py --apply                 # bookmarks
```

- **History** dedupes on `(url, visit_time ± 2 µs)`: the forward import
  stored Edge's integer µs as Cocoa REAL seconds, and the double
  round-trip drifts 1–2 µs, so exact-integer matching would re-insert
  ~half the migrated rows as near-duplicates. Inserted visits are
  local-only — Edge's own history sync does not replay raw DB rows.
- **Bookmarks** land under a single reversible `From Safari` folder in
  each profile's `other` root: insert-only, URL-dedupe against the whole
  target profile, existing Edge nodes never touched. Reading List and the
  `Edge Workspaces/` subtree are excluded by design. Unlike history,
  bookmark-file edits DO propagate through Edge's own sync to other
  devices.

### Bookmark cleanup across profile stores

```bash
uv run brm.py stores              # leaf + duplicate counts per store
uv run brm.py dedupe              # canonical-URL dedupe, dry run
uv run brm.py dedupe --apply      # removes losers, prunes empty folders
```

`brmlib.py` is the shared store model — canonicaliser, Safari plist /
Edge JSON adapters, browser-quit guards, timestamped backups. Stores:
`safari-personal`, `safari-work` (the `Work/` subtree — the only place the
work/personal boundary needs code enforcement), `edge-personal`,
`edge-work`. Canonical keys strip tracking params and normalise
scheme/host/`www`/trailing slash (`http://x` and `https://www.x/` count
as one bookmark); winners keep their stored URL — only duplicate losers
are deleted, nothing is ever rewritten.

---

## Safari iCloud history sync — how it actually works (macOS 27)

Empirically reverse-engineered 2026-10-08 after imports silently failed to
reach iCloud. The sync engine is a separate launch agent,
`/System/Cryptexes/App/usr/libexec/com.apple.Safari.History`, started with
Safari and persistent after it quits. Logging: `log stream --predicate
'process == "com.apple.Safari.History"'`.

- **Upload window**: the agent uploads only rows with
  `last_synced_generation < generation <= current_generation`.
- **Launch checkpoint**: at every Safari launch it sets
  `last_synced := current` and increments `current`. Any row at or below
  `current` — however it was written — is silently swallowed and will never
  upload. A watermark advance is therefore **not** evidence of an upload.
- **Correct stamp for out-of-band rows**: `current + 1`, written while Safari
  is quit. The next launch's checkpoint leaves the rows exactly at the new
  `current`, inside the window; the first idle cycle (~90 s after any
  browsing modification — a single opened page is enough) uploads them.
- **Verification signature**: after the cycle, `last_synced_generation` equals
  the stamp value exactly. A pure checkpoint lands on the pre-cycle
  `current`, so a watermark that stops precisely on your stamp is proof the
  rows went up.
- **Sync cadence**: idle cycles fire ~90 s after a history modification
  (config: `CloudHistoryRemoteConfiguration.plist`). Bulk uploads are
  batched across multiple `CKModifyRecordsOperation`s under a throttling
  schedule — a ~13k-row upload completed within minutes.
- **Benign noise**: the agent logs `openDirectory(...Profiles/<uuid>) -
  Operation not permitted` sandbox errors on every profile-directory touch.
  Reads and metadata writes still succeed; ignore them.
- **Red herring**: `profile_server_map` in the default profile's `metadata`
  does **not** route per-profile uploads (it is a migration ledger of
  external-UUID → server-UUID pairs). Profiles missing from it sync fine.

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
| `migrate.py` | **Main tool** — unified interactive migration: Chromium ↔ Chromium ↔ Safari, role-routed, dry-run default |
| `export_edge.py` | Edge history → `History.json` (Safari schema; kept for the Safari UI import route and `restamp_imports.py` matching) |
| `validator.py` | Validates a `History.json` against Apple's import schema |
| `direct_import_safari.py` | Import a `History.json` directly into a Safari profile's `History.db` (per-profile, stamps `current+1`; `import_items` is the shared write core) |
| `restamp_imports.py` | Re-stamp previously imported rows into the sync upload window (id-range + export-matched, conservative) |
| `sync_edge_bookmarks.py` | Delta-sync Edge bookmarks into the shared `Bookmarks.plist` (junk-filtered, dry-run first; `run_sync` is the shared core) |
| `dedupe_bookmarks.py` | Reading List stray repair + priority-aware bookmark dedupe + seeded-defaults strip |
| `reorganise_bookmarks.py` | Top-level reorganisation into scheme folders (September 2026) |
| `categorise_bookmarks.py` | URL classification ruleset used by the above |
| `safari_to_edge.py` | Reverse history pull: Safari profile DBs → Edge Beta History DBs (±2 µs tolerance dedupe, `--since` filter) |
| `safari_to_edge_bookmarks.py` | Reverse bookmark pull into a reversible `From Safari` folder per Edge profile (`pull_into_chromium` is the shared core) |
| `brm.py` | Bookmark CLI over the four profile stores: `stores`, `dedupe` |
| `brmlib.py` | Shared store model: canonicaliser, Safari/Edge adapters, quit-guards, backups |
| `archive/` | Superseded one-off scripts (see `archive/README.md`) |
