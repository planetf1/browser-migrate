#!/usr/bin/env python3
"""
brm — bookmark operations across browser profile stores.

Subcommands:
  stores               list the detected stores with leaf/duplicate counts
  dedupe               remove within-store duplicates by canonical URL
                       (dry run by default; --apply writes, with backup and
                       browser-quit guards)

Stores: safari-personal, safari-work, edge-personal, edge-work.
Reading List and Edge Workspaces are excluded by standing choice; the
work/personal boundary is never crossed (safari-work dedupes only within
the Work/ subtree, Edge stores are separate files).

Usage:
  uv run brm.py stores
  uv run brm.py dedupe                       # dry run, all stores
  uv run brm.py dedupe --store edge-personal # one store
  uv run brm.py dedupe --apply
"""

import argparse
import shutil
import sys

import brmlib as lib


def cmd_stores() -> int:
    tmpdir = None
    try:
        tree, zones, tmpdir = lib.read_safari()
        rows = []
        for name, leaves in zones.items():
            dups = sum(len(l) - 1 for _, _, l in lib.dedupe_plan(leaves))
            rows.append((name, len(leaves), dups))
        for name in lib.EDGE_DIRS:
            _, leaves, _ = lib.read_edge(name, tmpdir)
            dups = sum(len(l) - 1 for _, _, l in lib.dedupe_plan(leaves))
            rows.append((name, len(leaves), dups))
        print(f"{'store':<16} {'leaves':>7} {'dupes':>7}")
        for name, n, d in rows:
            print(f"{name:<16} {n:>7,} {d:>7,}")
        return 0
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def cmd_dedupe(store: str | None, apply: bool) -> int:
    tmpdir = None
    try:
        safari_tree, zones, tmpdir = lib.read_safari()
        jobs: list[tuple[str, list, str, object, str]] = []
        for name, leaves in zones.items():
            if store in (None, name):
                jobs.append((name, lib.dedupe_plan(leaves), "safari",
                             safari_tree, ""))
        for name in lib.EDGE_DIRS:
            if store in (None, name):
                tree, leaves, path = lib.read_edge(name, tmpdir)
                jobs.append((name, lib.dedupe_plan(leaves), "edge", tree, path))

        grand = 0
        for name, plan, kind, tree, path in jobs:
            n_losers = sum(len(l) for _, _, l in plan)
            print(f"── {name}: {n_losers} duplicate "
                  f"bookmark{'s' if n_losers != 1 else ''} "
                  f"across {len(plan)} canonical URLs"
                  f"{'  (DRY RUN)' if not apply else ''}")
            for key, winner, losers in plan[:6]:
                print(f"  {winner.url[:66]}")
                print(f"    keep  [{'/'.join(winner.path) or '(root)'}]")
                for loser in losers:
                    print(f"    drop  [{'/'.join(loser.path) or '(root)'}]"
                          f"{' — ' + loser.url[:48] if loser.url != winner.url else ''}")
            if len(plan) > 6:
                print(f"  …and {len(plan) - 6} more")
            if apply and plan:
                lib.apply_removals(plan)
                if kind == "safari":
                    for top in safari_tree.get("Children", []):
                        lib.prune_empty_plist(top, top=True)
                    backup = lib.write_safari(safari_tree, tmpdir)
                else:
                    for root in tree["roots"].values():
                        if isinstance(root, dict):
                            lib.prune_empty_json(root, top=True)
                    backup = lib.write_edge(path, tree)
                print(f"  written (backup: {backup})")
            grand += n_losers
            print()

        print(f"TOTAL duplicates: {grand:,}"
              f"{'' if apply else ' (dry run — add --apply to remove)'}")
        return 0
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(prog="brm")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stores")
    d = sub.add_parser("dedupe")
    d.add_argument("--store", choices=["safari-personal", "safari-work",
                                       "edge-personal", "edge-work"])
    d.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if a.cmd == "stores":
        return cmd_stores()
    return cmd_dedupe(a.store, a.apply)


if __name__ == "__main__":
    sys.exit(main())
