#!/usr/bin/env python3
"""
direct_import_safari.py — import a Safari History.json (Apple schema, as
produced by export_edge.py) directly into a Safari profile's History.db.

Bypasses Safari's import service, which on macOS 27 can mis-target a phantom
profile record after a profile delete/recreate (WBSHistoryErrorDomain Code=3,
"Failed to connect to history database") and never shows imported rows.

Usage:
    uv run direct_import_safari.py <History.json> <profile>

<profile> is "default" (~/Library/Safari) or a profile UUID (full or prefix)
under ~/Library/Containers/com.apple.Safari/Data/Library/Safari/Profiles/.

Safari MUST be quit. A consistent backup is written first as
<profile dir>/History.db.direct-import-<timestamp>.bak (VACUUM INTO, so WAL
contents are included). Rows are stamped at current_generation + 1: the next
Safari launch checkpoints last_synced := current, which leaves imported rows
exactly at the new current, inside the sync engine's upload window
((last_synced, current]), so the first idle cycle uploads them to iCloud.
Stamping at or below current gets them silently swallowed by the launch
checkpoint (see restamp_imports.py for the full mechanism).
"""

import json
import os
import sqlite3
import struct
import subprocess
import sys
import time
from urllib.parse import urlparse

COCOA_EPOCH = 978_307_200.0  # 2001-01-01 in unix seconds

# Public suffixes that take two labels (UK set; extend as needed).
UK_SUFFIXES = {"co.uk", "org.uk", "ac.uk", "gov.uk", "nhs.uk", "net.uk", "sch.uk"}


def die(msg: str) -> None:
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def domain_expansion_for(url: str) -> str | None:
    """Host minus public suffix, matching Safari's native values.

    login.w3.ibm.com -> 'login.w3.ibm', bbc.co.uk -> 'bbc',
    192.168.0.1 / file: / chrome-extension: -> None.
    """
    try:
        host = urlparse(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    parts = host.split(".")
    if len(parts) < 2:
        return None
    if parts[-2].isdigit() and parts[-1].isdigit():  # IP address
        return None
    last_two = ".".join(parts[-2:])
    drop = 2 if last_two in UK_SUFFIXES else 1
    if len(parts) <= drop:
        return None
    return ".".join(parts[:-drop])


def find_profile_dir(profile: str) -> str:
    if profile == "default":
        p = os.path.expanduser("~/Library/Safari")
        if not os.path.exists(os.path.join(p, "History.db")):
            die("no History.db in ~/Library/Safari")
        return p
    base = os.path.expanduser(
        "~/Library/Containers/com.apple.Safari/Data/Library/Safari/Profiles"
    )
    if not os.path.isdir(base):
        die(f"no Safari profiles dir at {base}")
    matches = [d for d in os.listdir(base) if d.startswith(profile)]
    if len(matches) != 1:
        die(f"profile prefix {profile!r} matched {len(matches)} profiles: {matches}")
    p = os.path.join(base, matches[0])
    if not os.path.exists(os.path.join(p, "History.db")):
        die(f"profile {matches[0]} has no History.db (never initialised?)")
    return p


def main() -> int:
    if len(sys.argv) != 3:
        die(f"usage: {sys.argv[0]} <History.json> <default|profile-uuid-prefix>")

    json_path = sys.argv[1]
    profile_dir = find_profile_dir(sys.argv[2])
    db_path = os.path.join(profile_dir, "History.db")

    if subprocess.run(["pgrep", "-x", "Safari"]).returncode == 0:
        die("Safari is running — quit it (⌘Q) first, then retry.")

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    meta = data.get("metadata", {})
    if meta.get("data_type") != "history" or "history" not in data:
        die(f"{json_path} is not a Safari history export (metadata: {meta})")
    items = data["history"]
    print(f"Loaded {len(items):,} items from {os.path.basename(json_path)}")

    conn = sqlite3.connect(db_path)
    try:
        # Consistent backup (VACUUM INTO folds the WAL in); timestamped so
        # repeated runs never collide (same fix as merge_safari_history.py).
        backup_path = f"{db_path}.direct-import-{time.strftime('%Y%m%d-%H%M%S')}.bak"
        conn.execute(f"VACUUM INTO {backup_path!r}")
        print(f"Backed up → {os.path.basename(backup_path)}")

        row = conn.execute(
            "SELECT value FROM metadata WHERE key = 'current_generation'"
        ).fetchone()
        gen = (int(row[0]) if row else 0) + 1
        print(f"Stamping visits with generation={gen} (current_generation + 1)")

        single_visit_blob = struct.pack("<I", 100)  # native single-visit shape
        inserted = skipped = 0
        for it in items:
            url = it["url"]
            title = (it.get("title") or "Untitled").strip() or "Untitled"
            time_cocoa = it["time_usec"] / 1_000_000.0 - COCOA_EPOCH
            if time_cocoa <= 0:
                skipped += 1
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO history_items "
                "(url, domain_expansion, visit_count, daily_visit_counts, "
                " weekly_visit_counts, autocomplete_triggers, "
                " should_recompute_derived_visit_counts, visit_count_score, status_code) "
                "VALUES (?, ?, 1, ?, NULL, NULL, 0, 100, 0)",
                (url, domain_expansion_for(url), single_visit_blob),
            )
            if cur.rowcount == 0:
                skipped += 1
                continue
            item_id = conn.execute(
                "SELECT id FROM history_items WHERE url = ?", (url,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO history_visits "
                "(history_item, visit_time, title, load_successful, http_non_get, "
                " synthesized, origin, generation, attributes, score) "
                "VALUES (?, ?, ?, 1, 0, 0, 0, ?, 0, 100)",
                (item_id, time_cocoa, title, gen),
            )
            inserted += 1
        conn.commit()
        print(f"✅ Imported {inserted:,} items ({skipped:,} skipped: pre-existing/invalid)")
        total = conn.execute("SELECT count(*) FROM history_items").fetchone()[0]
        print(f"Profile now holds {total:,} history items.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print("\nLaunch Safari, open the profile, ⌘⇧H to verify.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
