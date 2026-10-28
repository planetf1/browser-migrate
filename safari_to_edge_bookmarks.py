#!/usr/bin/env python3
"""
safari_to_edge_bookmarks.py — pull Safari bookmarks back into Edge Beta,
per profile, without duplicates.

Reverse of sync_edge_bookmarks.py. Reads the shared ~/Library/Safari/
Bookmarks.plist and delta-merges into each Edge Beta profile's Bookmarks
JSON:

  - Safari 'Work/' subtree          -> Edge 'Profile 1' (work)
  - everything else in shared tree  -> Edge 'Default' (personal)
  - 'Edge Workspaces/' is EXCLUDED  (its URLs already live in Edge's
    workspaces feature — importing them would duplicate)
  - Reading List is EXCLUDED by default (--reading-list to include it
    as a plain folder)

Merge semantics — deliberately conservative:
  - dedupe by URL against the whole target profile: anything Edge
    already has anywhere is skipped, so the round trip of the forward
    migration adds nothing;
  - insert-only: existing Edge nodes are never modified, moved or
    deleted;
  - new bookmarks land under ONE folder in the 'other' root named
    'From Safari', preserving the Safari folder path beneath it —
    delete that folder to undo a run; re-running is idempotent.

Safari may stay running (the plist is snapshot-copied first). For
--apply, Edge must be fully quit; each target Bookmarks file gets a
timestamped backup first.

Usage:
  uv run safari_to_edge_bookmarks.py                  # dry run
  uv run safari_to_edge_bookmarks.py --reading-list   # include RL
  uv run safari_to_edge_bookmarks.py --apply
  uv run safari_to_edge_bookmarks.py --profile work
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections import defaultdict

import plistlib

EDGE_ROOT = os.path.expanduser("~/Library/Application Support/Microsoft Edge Beta")
PLIST_PATH = os.path.expanduser("~/Library/Safari/Bookmarks.plist")
INBOX_NAME = "From Safari"
CHROMIUM_EPOCH_OFFSET = 11_644_473_600  # 1601-01-01 -> unix, in seconds

PROFILE_MAP = {
    "personal": ("Default", "nigel@cherrybyte.me.uk"),
    "work": ("Profile 1", "jonesn@uk.ibm.com"),
}


def die(msg: str) -> None:
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def edge_identity(edge_dir: str) -> str:
    prefs = os.path.join(EDGE_ROOT, edge_dir, "Preferences")
    try:
        with open(prefs, "r", encoding="utf-8") as f:
            return json.load(f).get("account_info", [{}])[0].get("email", "(signed out)")
    except (OSError, json.JSONDecodeError):
        return "(unknown)"


def walk(node: dict, path: list[str]):
    """Yield (title, url, path) for every leaf under node."""
    if node.get("WebBookmarkType") == "WebBookmarkTypeLeaf":
        url = node.get("URLString", "")
        title = node.get("URIDictionary", {}).get("title", "") or url
        if url:
            yield title, url, path
    elif node.get("WebBookmarkType") == "WebBookmarkTypeList":
        title = node.get("Title") or ""
        for child in node.get("Children", []):
            yield from walk(child, path + ([title] if title else []))


def collect_safari_leaves(root: dict, include_reading_list: bool):
    """Split the shared tree into (personal_leaves, work_leaves, excluded)."""
    personal, work, excluded = [], [], []
    for top in root.get("Children", []):
        if top.get("WebBookmarkType") != "WebBookmarkTypeList":
            continue
        title = top.get("Title") or ""
        if title in ("Reading List", "com.apple.ReadingList"):
            bucket = personal if include_reading_list else excluded
        elif title == "Work":
            bucket = work
        elif title == "Edge Workspaces":
            bucket = excluded
        else:
            bucket = personal
        for leaf in walk(top, []):
            bucket.append(leaf)
    return personal, work, excluded


def edge_urls_and_ids(root: dict) -> tuple[set[str], int]:
    """All URL strings in the Edge tree, and the max numeric node id."""
    urls: set[str] = set()
    max_id = 0

    def visit(node):
        nonlocal max_id
        if "id" in node:
            try:
                max_id = max(max_id, int(node["id"]))
            except ValueError:
                pass
        if node.get("type") == "url" and node.get("url"):
            urls.add(node["url"])
        for child in node.get("children", []):
            visit(child)

    for r in root.get("roots", {}).values():
        if isinstance(r, dict):
            visit(r)
    return urls, max_id


def build_inbox_tree(new_leaves: list[tuple[str, str, list[str]]]) -> dict:
    """Fold deduped leaves into a nested folder tree under the inbox."""
    tree: dict = {"folders": defaultdict(dict), "leaves": []}
    seen_urls: set[str] = set()
    for title, url, path in new_leaves:
        if url in seen_urls:
            continue
        seen_urls.add(url)
        node = tree
        for folder in path:
            node = node["folders"].setdefault(folder, {"folders": defaultdict(dict), "leaves": []})
        node["leaves"].append({"name": title, "url": url})
    return tree


def to_chromium(tree: dict, next_id: list[int]) -> list[dict]:
    """Convert the inbox tree to Chromium bookmark nodes (depth-first ids)."""
    now_us = str(int((time.time() + CHROMIUM_EPOCH_OFFSET) * 1e6))
    out = []
    for name, sub in tree["folders"].items():
        children = to_chromium(sub, next_id)
        if not children:
            continue  # never create empty folders
        next_id[0] += 1
        out.append({
            "date_added": now_us, "date_modified": now_us,
            "guid": str(uuid.uuid4()), "id": str(next_id[0]),
            "name": name, "type": "folder", "children": children,
        })
    for leaf in tree["leaves"]:
        next_id[0] += 1
        out.append({
            "date_added": now_us, "guid": str(uuid.uuid4()),
            "id": str(next_id[0]), "name": leaf["name"],
            "type": "url", "url": leaf["url"],
        })
    return out


def pull_into_chromium(leaves: list[tuple[str, str, list[str]]], bm_path: str,
                        apply: bool, inbox_name: str = INBOX_NAME) -> int:
    """Insert Safari leaves not already in the target Chromium profile.

    Reads from a snapshot (target browser may be running); on apply, takes a
    timestamped backup and writes under a single reversible inbox folder in
    the 'other' root. Shared core of the CLI and migrate.py.
    Returns the number of bookmarks added (or that would be, in dry run).
    """
    tmpdir = tempfile.mkdtemp(prefix="s2ebm-core-")
    try:
        snap = os.path.join(tmpdir, "Bookmarks.json")
        shutil.copy2(bm_path, snap)
        with open(snap, "r", encoding="utf-8") as f:
            tree = json.load(f)

        existing_urls, max_id = edge_urls_and_ids(tree)
        new_leaves = [(t, u, p) for t, u, p in leaves if u not in existing_urls]
        inbox = build_inbox_tree(new_leaves)
        by_top = defaultdict(int)
        for _, _, path in new_leaves:
            by_top[path[0] if path else "(root)"] += 1

        print(f"  Safari leaves considered: {len(leaves):,}")
        print(f"  already present (URL dedupe): {len(leaves) - len(new_leaves):,}")
        print(f"  would add: {len(new_leaves):,} bookmarks"
              f"{'  (DRY RUN)' if not apply else ''}")
        if by_top:
            print("  by Safari folder: "
                  + "  ".join(f"{k}×{v}" for k, v in
                              sorted(by_top.items(), key=lambda kv: -kv[1])[:8]))
        for title, url, path in new_leaves[:5]:
            loc = "/".join(path[:3]) or "(root)"
            print(f"  e.g. [{loc}] {title[:40]} — {url[:64]}")

        if apply and new_leaves:
            ts = time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(bm_path, f"{bm_path}.safari-import-{ts}.bak")
            nodes = to_chromium(inbox, [max_id])
            other = tree["roots"]["other"]
            inbox_node = next((c for c in other.get("children", [])
                               if c.get("name") == inbox_name
                               and c.get("type") == "folder"), None)
            if inbox_node is None:
                max_id += 1
                inbox_node = {
                    "date_added": str(int((time.time() + CHROMIUM_EPOCH_OFFSET) * 1e6)),
                    "date_modified": str(int((time.time() + CHROMIUM_EPOCH_OFFSET) * 1e6)),
                    "guid": str(uuid.uuid4()), "id": str(max_id),
                    "name": inbox_name, "type": "folder", "children": [],
                }
                other.setdefault("children", []).append(inbox_node)
            inbox_node.setdefault("children", []).extend(nodes)
            tree.pop("checksum", None)  # Chromium recomputes on next save
            with open(bm_path, "w", encoding="utf-8") as f:
                json.dump(tree, f, indent=3, ensure_ascii=False)
            print(f"  added {len(new_leaves):,} bookmarks under "
                  f"'{inbox_name}' in 'other' (backup: Bookmarks.safari-import-{ts}.bak)")
        return len(new_leaves)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", choices=["personal", "work"], default=None)
    ap.add_argument("--reading-list", action="store_true",
                    help="include Safari Reading List as a plain folder")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(PLIST_PATH):
        die(f"missing {PLIST_PATH}")
    tmpdir = tempfile.mkdtemp(prefix="s2ebm-")
    snap = os.path.join(tmpdir, "Bookmarks.plist")
    shutil.copy2(PLIST_PATH, snap)
    try:
        with open(snap, "rb") as f:
            plist = plistlib.load(f)
    except plistlib.InvalidFileException:
        die("Bookmarks.plist snapshot failed to parse — is Safari mid-write? "
            "Retry, or quit Safari first.")

    if a.apply:
        r = subprocess.run(["pgrep", "-if", "Microsoft Edge Beta.app"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            die("Edge is running — quit it (⌘Q) first, then retry.")

    personal, work, excluded = collect_safari_leaves(plist, a.reading_list)
    print(f"Shared plist: {len(personal):,} personal leaves, "
          f"{len(work):,} work leaves, {len(excluded):,} excluded "
          f"(Edge Workspaces{'' if a.reading_list else ' + Reading List'})\n")

    selected = [a.profile] if a.profile else ["personal", "work"]
    grand = 0
    for prof in selected:
        edge_dir, expected_email = PROFILE_MAP[prof]
        email = edge_identity(edge_dir)
        if email != expected_email:
            die(f"Edge Beta {edge_dir!r} resolved to {email!r}, expected "
                f"{expected_email!r} — profile layout changed; refusing to "
                f"write. Update PROFILE_MAP.")
        bm_path = os.path.join(EDGE_ROOT, edge_dir, "Bookmarks")
        if not os.path.exists(bm_path):
            die(f"missing {bm_path}")

        safari_leaves = personal if prof == "personal" else work
        print(f"── {prof}: Safari shared tree → Edge Beta {edge_dir!r} ({email}) ──")
        grand += pull_into_chromium(safari_leaves, bm_path, a.apply)
        print()

    print(f"TOTAL would add: {grand:,} bookmarks"
          f"{'' if a.apply else ' (dry run — add --apply to write)'}")
    shutil.rmtree(tmpdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
