#!/usr/bin/env python3
"""
restamp_imports.py — re-stamp imported history rows into the sync engine's
upload window so iCloud actually picks them up.

Mechanism (macOS 27, com.apple.Safari.History agent), verified empirically
2026-10-08:

  * The agent uploads only rows with
        last_synced_generation < generation <= current_generation
  * At every Safari launch it checkpoints last_synced := current and
    increments current — silently swallowing any row at or below current,
    however it was written. This is why imports stamped at
    current_generation (or at the watermark) never reach iCloud.
  * Rows stamped at current_generation + 1 while Safari is quit land exactly
    at the new current after the next launch checkpoint, inside the upload
    window; the first idle cycle (~90 s after any browsing modification)
    uploads them. Rows stamped above current+1 are skipped until current
    catches up.

Row selection is deliberately conservative: only visit ids inside known
import-batch ranges (derived from pre-import backups) whose (url,
visit_time) matches an Edge export — i.e. exactly the rows the import
scripts inserted. Native Safari rows are never touched, so nothing already
uploaded gets duplicated.

Usage:
  uv run restamp_imports.py <default|uuid-prefix> --range LO:HI \
      --export Edge.json [--export Older.json] [--more ranges...] [--apply]

--range is visit-id range, exclusive of LO, inclusive of HI (LO is the max
id in the pre-import backup). Multiple --range/--export flags are ANDed as
a union of ranges and a union of exports. Dry run by default; Safari must
be quit for --apply.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time

COCOA_EPOCH = 978_307_200.0


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
    return os.path.join(base, matches[0])


def parse_range(spec: str) -> tuple[int, int]:
    lo, _, hi = spec.partition(":")
    try:
        return int(lo), int(hi)
    except ValueError:
        die(f"bad --range {spec!r} (want LO:HI)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("profile")
    ap.add_argument("--range", dest="ranges", action="append", required=True)
    ap.add_argument("--export", dest="exports", action="append", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    ranges = [parse_range(r) for r in a.ranges]

    profile_dir = find_profile_dir(a.profile)
    db_path = os.path.join(profile_dir, "History.db")

    pairs: set[tuple[str, float]] = set()
    for path in a.exports:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("metadata", {}).get("data_type") != "history":
            die(f"{path} is not a Safari history export")
        for it in data["history"]:
            t = round(it["time_usec"] / 1_000_000.0 - COCOA_EPOCH, 3)
            if t > 0:
                pairs.add((it["url"], t))
    print(f"Export key set: {len(pairs):,} (url, time) pairs")

    conn = sqlite3.connect(db_path)
    meta = dict(conn.execute("SELECT key, value FROM metadata"))
    current = int(meta["current_generation"])
    last_synced = int(meta["last_synced_generation"])
    target = current + 1
    print(f"current={current}  last_synced={last_synced}  -> stamp {target}")

    where = " OR ".join("(v.id > ? AND v.id <= ?)" for _ in ranges)
    args = [x for r in ranges for x in r]
    rows = conn.execute(
        f"SELECT v.id, v.generation, v.visit_time, h.url "
        f"FROM history_visits v JOIN history_items h ON h.id = v.history_item "
        f"WHERE {where}",
        args,
    ).fetchall()
    in_range = len(rows)
    matched = [
        (rid, gen, url)
        for rid, gen, vt, url in rows
        if (url, round(vt, 3)) in pairs
    ]
    print(f"Visits in id ranges: {in_range:,}; matched to exports: {len(matched):,} "
          f"(unmatched natives left alone: {in_range - len(matched):,})")

    stranded = [rid for rid, gen, _ in matched if gen <= last_synced]
    above = [(rid, gen) for rid, gen, _ in matched if gen > current]
    print(f"Matched rows at/below watermark (stranded, will re-stamp): {len(stranded):,}")
    if above:
        print(f"Matched rows above current (skipped as future until current "
              f"catches up — re-stamping down into window): "
              f"{', '.join(f'{g}:{len([1 for _, gg in above if gg == g])}' for g in sorted({g for _, g in above}))}")

    from collections import Counter
    gens = Counter(gen for _, gen, _ in matched)
    print("Generation spread of matched rows:",
          ", ".join(f"{g}×{n}" for g, n in sorted(gens.items())[:8]),
          "…" if len(gens) > 8 else "")
    for rid, gen, url in matched[:5]:
        print(f"  e.g. id {rid} gen {gen}: {url[:70]}")

    if not a.apply:
        print("\nDRY RUN — nothing changed. Re-run with --apply.")
        return 0

    if subprocess.run(["pgrep", "-x", "Safari"]).returncode == 0:
        die("Safari is running — quit it (⌘Q) first, then retry.")

    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_path = f"{db_path}.restamp-{ts}.bak"
    conn.execute(f"VACUUM INTO {backup_path!r}")
    ids = [rid for rid, _, _ in matched]
    conn.executemany(
        f"UPDATE history_visits SET generation = {target} WHERE id = ?",
        [(rid,) for rid in ids],
    )
    conn.commit()
    done = conn.execute(
        "SELECT count(*) FROM history_visits WHERE generation = ?", (target,)
    ).fetchone()[0]
    print(f"\n✅ Re-stamped {len(ids):,} rows -> generation {target} "
          f"({done} rows now at target)")
    print(f"Backup → {os.path.basename(backup_path)}")
    print("Next: launch Safari, browse one page, wait ~90 s for the idle "
          "cycle; last_synced_generation should advance to", target)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
