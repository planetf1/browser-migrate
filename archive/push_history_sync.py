#!/usr/bin/env python3
"""Push directly-written history rows to iCloud by bumping their sync generation.

The macOS 27 history sync agent (com.apple.Safari.History) uploads only rows
with last_synced_generation < generation <= current_generation, and at every
Safari launch checkpoints last_synced := current — silently swallowing any
row at or below current, however it was written. Rows written out-of-band
(direct_import_safari.py, merge_safari_history.py) must therefore be
re-stamped at current_generation + 1: the next launch's checkpoint leaves
them exactly at the new current, inside the upload window, and the first
idle cycle (~90 s after any browsing modification) uploads them to iCloud.

Stamping at current (or at the watermark) does NOT work — the launch
checkpoint swallows those rows and the watermark advance looks like success
while nothing reaches the server. Verified empirically 2026-10-08; see
restamp_imports.py for the conservative row-selection variant.

Usage:
  uv run push_history_sync.py <default|uuid-prefix> --gen 126      # exact generation
  uv run push_history_sync.py <default|uuid-prefix> --max-gen 5    # everything at/below
  add --apply to write (dry run by default; Safari must be quit)
"""
import argparse
import os
import sqlite3
import subprocess
import sys
import time


def resolve(selector: str) -> str:
    if selector == "default":
        return os.path.expanduser("~/Library/Safari/History.db")
    base = os.path.expanduser(
        "~/Library/Containers/com.apple.Safari/Data/Library/Safari/Profiles")
    hits = [d for d in os.listdir(base) if d.startswith(selector)]
    if len(hits) != 1:
        sys.exit(f"selector {selector!r} matched {len(hits)} profiles: {hits}")
    db = os.path.join(base, hits[0], "History.db")
    if not os.path.exists(db):
        sys.exit(f"no History.db in {hits[0]}")
    return db


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("profile", help="'default' or a profile UUID prefix")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--gen", type=int, help="bump rows with exactly this generation")
    g.add_argument("--max-gen", type=int, help="bump rows with generation <= this")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    if subprocess.run(["pgrep", "-x", "Safari"]).returncode == 0:
        sys.exit("Safari is running - quit it first.")
    db = resolve(a.profile)
    c = sqlite3.connect(db)
    meta = dict(c.execute("SELECT key, value FROM metadata"))
    last_synced = int(meta["last_synced_generation"])
    current = int(meta["current_generation"])
    target = current + 1
    if target <= last_synced:
        sys.exit(f"current_generation ({current}) <= last_synced_generation "
                 f"({last_synced}) - nothing to gain")
    if a.gen is not None:
        sel, args = "generation = ?", (a.gen,)
    else:
        sel, args = "generation <= ?", (a.max_gen,)
    n = c.execute(f"SELECT count(*) FROM history_visits WHERE {sel}", args).fetchone()[0]
    print(f"profile db : {db}")
    print(f"watermark  : last_synced={last_synced}  current={current}  -> bump to {target} (current+1)")
    print(f"rows matched: {n}")
    if n == 0:
        c.close()
        sys.exit("no rows matched - nothing to do")
    for (u,) in c.execute(
            "SELECT i.url FROM history_visits v JOIN history_items i ON i.id=v.history_item "
            f"WHERE {sel} LIMIT 5", args):
        print(f"   e.g. {u[:90]}")
    if not a.apply:
        print("\nDRY RUN - re-run with --apply")
        c.close()
        return
    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = db + f".push-{ts}.bak"
    c.execute("VACUUM INTO ?", (bak,))
    c.execute(f"UPDATE history_visits SET generation = ? WHERE {sel}", (target, *args))
    c.commit()
    done = c.execute(
        "SELECT count(*) FROM history_visits WHERE generation = ?", (target,)).fetchone()[0]
    print(f"backup: {bak}")
    print(f"bumped: {c.total_changes} visits -> generation {target} ({done} rows now at target)")
    c.close()


if __name__ == "__main__":
    main()
