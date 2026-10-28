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
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
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


def import_items(db_path: str, items: list[tuple[str, str, float]],
                  apply: bool = True) -> tuple[int, int, int | None]:
    """Write (url, title, cocoa_time) rows into a Safari profile History.db.

    Shared core of the CLI and migrate.py. With apply=True: requires Safari
    quit, takes a VACUUM INTO backup, stamps visits at current_generation + 1
    and commits. With apply=False: counts what would be new against a
    read-only snapshot (Safari may be running); nothing is written.

    Returns (inserted, skipped, stamp); stamp is None unless applied.
    """
    single_visit_blob = struct.pack("<I", 100)  # native single-visit shape
    if apply:
        conn = sqlite3.connect(db_path)
        # Consistent backup (VACUUM INTO folds the WAL in); timestamped so
        # repeated runs never collide, with a counter if two land in the
        # same second (VACUUM INTO refuses to overwrite).
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup_path = f"{db_path}.direct-import-{ts}.bak"
        n = 1
        while os.path.exists(backup_path):
            backup_path = f"{db_path}.direct-import-{ts}-{n}.bak"
            n += 1
        conn.execute(f"VACUUM INTO {backup_path!r}")
        print(f"  Backed up → {os.path.basename(backup_path)}")
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = 'current_generation'"
        ).fetchone()
        stamp = (int(row[0]) if row else 0) + 1
        print(f"  Stamping visits with generation={stamp} (current_generation + 1)")
    else:
        tmpdir = tempfile.mkdtemp(prefix="safari-count-")
        snap = db_path
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(db_path + ext):
                shutil.copy2(db_path + ext, os.path.join(tmpdir, "db" + ext))
        snap = os.path.join(tmpdir, "db")
        conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
        stamp = None

    try:
        existing = {row[0] for row in conn.execute("SELECT url FROM history_items")}
        inserted = skipped = 0
        for url, title, time_cocoa in items:
            title = (title or "Untitled").strip() or "Untitled"
            if time_cocoa <= 0 or url in existing:
                skipped += 1
                continue
            if apply:
                conn.execute(
                    "INSERT INTO history_items "
                    "(url, domain_expansion, visit_count, daily_visit_counts, "
                    " weekly_visit_counts, autocomplete_triggers, "
                    " should_recompute_derived_visit_counts, visit_count_score, status_code) "
                    "VALUES (?, ?, 1, ?, NULL, NULL, 0, 100, 0)",
                    (url, domain_expansion_for(url), single_visit_blob),
                )
                item_id = conn.execute(
                    "SELECT id FROM history_items WHERE url = ?", (url,)
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO history_visits "
                    "(history_item, visit_time, title, load_successful, http_non_get, "
                    " synthesized, origin, generation, attributes, score) "
                    "VALUES (?, ?, ?, 1, 0, 0, 0, ?, 0, 100)",
                    (item_id, time_cocoa, title, stamp),
                )
            existing.add(url)
            inserted += 1
        if apply:
            conn.commit()
    except Exception:
        if apply:
            conn.rollback()
        raise
    finally:
        conn.close()
        if not apply:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return inserted, skipped, stamp


def main() -> int:
    if len(sys.argv) != 3:
        die(f"usage: {sys.argv[0]} <History.json> <default|profile-uuid-prefix>")

    json_path = sys.argv[1]
    profile_dir = find_profile_dir(sys.argv[2])
    db_path = os.path.join(profile_dir, "History.db")

    if subprocess.run(["pgrep", "-x", "Safari"],
                      capture_output=True).returncode == 0:
        die("Safari is running — quit it (⌘Q) first, then retry.")

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    meta = data.get("metadata", {})
    if meta.get("data_type") != "history" or "history" not in data:
        die(f"{json_path} is not a Safari history export (metadata: {meta})")
    items = [(it["url"], it.get("title") or "Untitled",
              it["time_usec"] / 1_000_000.0 - COCOA_EPOCH)
             for it in data["history"]]
    print(f"Loaded {len(items):,} items from {os.path.basename(json_path)}")

    inserted, skipped, _ = import_items(db_path, items, apply=True)
    print(f"✅ Imported {inserted:,} items ({skipped:,} skipped: pre-existing/invalid)")
    total = sqlite3.connect(db_path).execute(
        "SELECT count(*) FROM history_items").fetchone()[0]
    print(f"Profile now holds {total:,} history items.")
    print("\nLaunch Safari, open the profile, ⌘⇧H to verify.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
