#!/usr/bin/env python3
"""
merge_safari_history.py — merge one Safari profile's history into another's
History.db (row-level, URL-deduplicated). For consolidating profiles before
deleting them.

Usage:
    uv run merge_safari_history.py <source> <target>

<source>/<target> are "default" (~/Library/Safari) or a profile UUID prefix
under ~/Library/Containers/com.apple.Safari/Data/Library/Safari/Profiles/.

Safari MUST be quit. A consistent backup of the TARGET db is written first
(<target>/History.db.merge.bak). Visits copied from the source are re-stamped
with the target's last_synced_generation so the sync engine treats them as
settled (no iCloud upload). Exact (url, visit_time) duplicates are skipped.
"""

import os
import sqlite3
import subprocess
import sys
import time


def die(msg: str) -> None:
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def find_profile_dir(profile: str) -> str:
    if profile == "default":
        p = os.path.expanduser("~/Library/Safari")
        if not os.path.exists(os.path.join(p, "History.db")):
            die("no History.db in ~/Library/Safari")
        return p
    base = os.path.expanduser(
        "~/Library/Containers/com.apple.Safari/Data/Library/Safari/Profiles"
    )
    matches = [d for d in os.listdir(base) if d.startswith(profile)]
    if len(matches) != 1:
        die(f"profile prefix {profile!r} matched {len(matches)} profiles: {matches}")
    p = os.path.join(base, matches[0])
    if not os.path.exists(os.path.join(p, "History.db")):
        die(f"profile {matches[0]} has no History.db")
    return p


def last_synced_generation(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM metadata WHERE key = 'last_synced_generation'"
    ).fetchone()
    return int(row[0]) if row else 1


def main() -> int:
    if len(sys.argv) != 3:
        die(f"usage: {sys.argv[0]} <source> <target>")
    if sys.argv[1] == sys.argv[2]:
        die("source and target must differ")

    if subprocess.run(["pgrep", "-x", "Safari"]).returncode == 0:
        die("Safari is running — quit it (⌘Q) first, then retry.")

    src_dir = find_profile_dir(sys.argv[1])
    dst_dir = find_profile_dir(sys.argv[2])
    dst_db = os.path.join(dst_dir, "History.db")

    dst = sqlite3.connect(dst_db)
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = f"{dst_db}.merge-{stamp}.bak"
        dst.execute(f"VACUUM INTO {backup!r}")
        print(f"Backed up target → {os.path.basename(backup)}")
        gen = last_synced_generation(dst)
        print(f"Re-stamping source visits with generation={gen} (target last_synced_generation)")

        src = sqlite3.connect(f"file:{os.path.join(src_dir, 'History.db')}?mode=ro", uri=True)
        try:
            dst_map = dict(dst.execute("SELECT url, id FROM history_items"))
            items_merged = items_new = visits_copied = visits_dup = 0

            src_rows = src.execute(
                "SELECT hi.url, hi.domain_expansion, hi.daily_visit_counts, "
                " hi.visit_count, hi.visit_count_score, hv.visit_time, hv.title "
                "FROM history_items hi JOIN history_visits hv ON hv.history_item = hi.id "
                "ORDER BY hi.id, hv.visit_time"
            ).fetchall()

            new_items: dict[str, list] = {}  # url -> [domain_expansion, blob, vc, score, [(t, title)]]
            for url, de, blob, vc, score, vtime, title in src_rows:
                bucket = new_items.setdefault(url, [de, blob, vc or 0, score, []])
                bucket[4].append((vtime, title))

            for url, (de, blob, vc, score, visits) in new_items.items():
                if not visits:
                    continue
                if url in dst_map:
                    item_id = dst_map[url]
                    for vtime, title in visits:
                        dup = dst.execute(
                            "SELECT 1 FROM history_visits WHERE history_item = ? "
                            "AND visit_time BETWEEN ? AND ? LIMIT 1",
                            (item_id, vtime - 1.0, vtime + 1.0),
                        ).fetchone()
                        if dup:
                            visits_dup += 1
                        else:
                            dst.execute(
                                "INSERT INTO history_visits "
                                "(history_item, visit_time, title, load_successful, "
                                " http_non_get, synthesized, origin, generation, attributes, score) "
                                "VALUES (?, ?, ?, 1, 0, 0, 0, ?, 0, ?)",
                                (item_id, vtime, title, gen, score or 100),
                            )
                            visits_copied += 1
                    items_merged += 1
                    dst.execute(
                        "UPDATE history_items SET should_recompute_derived_visit_counts = 1, "
                        "visit_count = (SELECT count(*) FROM history_visits WHERE history_item = ?) "
                        "WHERE id = ?",
                        (item_id, item_id),
                    )
                else:
                    cur = dst.execute(
                        "INSERT INTO history_items "
                        "(url, domain_expansion, visit_count, daily_visit_counts, "
                        " weekly_visit_counts, autocomplete_triggers, "
                        " should_recompute_derived_visit_counts, visit_count_score, status_code) "
                        "VALUES (?, ?, ?, ?, NULL, NULL, 0, ?, 0)",
                        (url, de, len(visits), blob, score or 100),
                    )
                    item_id = cur.lastrowid
                    for vtime, title in visits:
                        dst.execute(
                            "INSERT INTO history_visits "
                            "(history_item, visit_time, title, load_successful, "
                            " http_non_get, synthesized, origin, generation, attributes, score) "
                            "VALUES (?, ?, ?, 1, 0, 0, 0, ?, 0, ?)",
                            (item_id, vtime, title, gen, score or 100),
                        )
                        visits_copied += 1
                    items_new += 1
                    dst_map[url] = item_id

            dst.commit()
            total = dst.execute("SELECT count(*) FROM history_items").fetchone()[0]
            tv = dst.execute("SELECT count(*) FROM history_visits").fetchone()[0]
            print(f"✅ Merged: {items_new:,} new items, {items_merged:,} existing items topped up")
            print(f"   visits copied: {visits_copied:,} (duplicates skipped: {visits_dup:,})")
            print(f"Target profile now holds {total:,} items / {tv:,} visits.")
        finally:
            src.close()
    except Exception:
        dst.rollback()
        raise
    finally:
        dst.close()
    print("\nLaunch Safari and verify ⌘⇧H in the target profile before deleting the source.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
