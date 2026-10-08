#!/usr/bin/env python3
"""
dedupe_bookmarks.py — priority-aware duplicate cleanup for Safari's shared
Bookmarks.plist, plus repairs for the Oct 2026 iCloud device merge:

  1. Relocate stray Reading List items (leaves carrying ReadingListNonSync /
     previewText outside the Reading List folder) back into it.
  2. Dedupe the Reading List itself by URL.
  3. Remove duplicate bookmark leaves by canonical URL, keeping the copy in
     the best folder. Priority (best first): Work > scheme folders >
     Conferences/Family > device-merged folders > bare top level > Bar.
     'Edge Workspaces', Reading List and History are never touched here.
  4. --empty-bar: remove Apple's seeded default favourites from the
     Bookmarks Bar, and from any folder holding a whole seeded cluster
     (>= 8 of the bar's current URLs) — e.g. 'Cars & EV' after the merge.

Emptied subfolders are pruned; emptied TOP-LEVEL folders are reported only
(delete those in Safari's UI so the deletion syncs to devices).

Usage:
    uv run dedupe_bookmarks.py [--empty-bar] [--apply]

Safari MUST be quit for --apply.
"""

import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlparse

PLIST = os.path.expanduser("~/Library/Safari/Bookmarks.plist")

SCHEME = {"Travel", "Programming", "AI", "Tech", "Home & Network", "Health",
          "Entertainment", "News", "Money & Admin", "Shopping & Food",
          "Cars & EV", "Jobs", "Community", "Unsorted", "Security"}
KEPT_WHOLE = {"Conferences", "Family"}
NEVER_TOUCH = {"Edge Workspaces"}  # + Reading List (by identifier) + History
STRUCTURAL = {"BookmarksBar", "BookmarksMenu", "Bookmarks", "History"}
SEED_CLUSTER_MIN = 8  # a folder holding >= this many of the bar's URLs is a seeding artefact


def die(msg: str) -> None:
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def norm(url: str) -> str:
    try:
        p = urlparse(url.lower().rstrip("/"))
        host = (p.hostname or "").removeprefix("www.")
        return f"{host}{p.path}".rstrip("/")
    except ValueError:
        return url.lower().rstrip("/")


def is_mojibake(s: str) -> bool:
    return "â€" in s or "Â" in s


RL_AGGREGATE_RE = re.compile(r"\s\[\+\d+\]$")


def is_rl_stray(node: dict) -> bool:
    """Reading List entry dumped into the bookmark tree by the Sept import/
    reconciliation: aggregated '[+N]' title, or any leaf at bare top level
    (the Sept cleanup left zero of those). ReadingListNonSync is useless as a
    signal — it sits on nearly every leaf post-CloudKit-migration."""
    if is_rl_folder(node):
        return False
    title = (node.get("URIDictionary") or {}).get("title", "")
    return bool(RL_AGGREGATE_RE.search(title))


def title_of(node: dict) -> str:
    return (node.get("URIDictionary") or {}).get("title", "") or node.get("Title", "")


def is_rl_folder(node: dict) -> bool:
    return node.get("WebBookmarkIdentifier") == "com.apple.ReadingList" \
        or node.get("Title") == "com.apple.ReadingList"


def folder_class(title: str | None) -> int:
    """Lower = better. None-title = bare top-level leaf."""
    if title == "Work":
        return 0
    if title in SCHEME:
        return 1
    if title in KEPT_WHOLE:
        return 2
    if title in NEVER_TOUCH or title in ("BookmarksBar", "BookmarksMenu", "Bookmarks"):
        return 3
    if title is None:
        return 5  # bare top-level leaf
    return 4  # device-merged / unknown folders


def index_tree(root: dict):
    """parent map + document order for every leaf below top-level folders."""
    parent_of: dict[int, dict] = {}
    order: list[tuple[dict, str | None]] = []  # (leaf, top-level title)

    def index(node: dict, top: str | None) -> None:
        for k in node.get("Children", []):
            parent_of[id(k)] = node
            if k.get("WebBookmarkType") == "WebBookmarkTypeLeaf":
                order.append((k, top))
            else:
                index(k, top)

    for ch in root.get("Children", []):
        t = ch.get("Title")
        if ch.get("WebBookmarkType") == "WebBookmarkTypeLeaf":
            order.append((ch, None))
            parent_of[id(ch)] = root
        else:
            index(ch, t)
    return parent_of, order


def prune_emptied(node: dict, parent_of: dict, root: dict, pruned: list) -> None:
    """Remove now-empty folders up to (not including) the top level."""
    while node is not root and node.get("WebBookmarkType") == "WebBookmarkTypeList" \
            and not node.get("Children"):
        p = parent_of.get(id(node))
        if p is None:
            break
        p["Children"].remove(node)
        pruned.append(node.get("Title", "?"))
        node = p


def main() -> int:
    apply = "--apply" in sys.argv
    empty_bar = "--empty-bar" in sys.argv
    if apply and subprocess.run(["pgrep", "-x", "Safari"]).returncode == 0:
        die("Safari is running — quit it (⌘Q) first, then retry with --apply.")

    with open(PLIST, "rb") as f:
        root = plistlib.load(f)
    top = root.get("Children", [])

    # ---- 1. relocate stray Reading List items -------------------------------
    rl_folder = next((ch for ch in top if is_rl_folder(ch)), None)
    if rl_folder is None:
        die("no Reading List folder found — aborting rather than guessing.")
    rl_children = rl_folder.setdefault("Children", [])
    strays: list[dict] = []

    def collect_strays(node: dict, in_rl: bool, top_title: str | None) -> None:
        for k in node.get("Children", []):
            if k.get("WebBookmarkType") == "WebBookmarkTypeLeaf":
                if not in_rl and (is_rl_stray(k) or top_title is None):
                    strays.append(k)
            else:
                collect_strays(k, in_rl or is_rl_folder(k),
                               k.get("Title") if node is root else top_title)

    collect_strays(root, False, None)
    # Guard: a broken stray detector (e.g. a metadata key that sits on every
    # leaf) must abort, not relocate the whole tree into the Reading List.
    n_leaves = sum(1 for _, t in index_tree(root)[1])
    if len(strays) > n_leaves * 0.25:
        die(f"stray detector matched {len(strays)}/{n_leaves} leaves — refusing "
            "to relocate; the detection signal is wrong, not the tree.")
    # detach each stray from its parent, then append to the Reading List
    parent_of_pre, _ = index_tree(root)
    for s in strays:
        p = parent_of_pre.get(id(s))
        if p is not None and s in p.get("Children", []):
            p["Children"].remove(s)
        rl_children.append(s)
    print(f"Reading List strays relocated to the Reading List: {len(strays)}")

    # ---- 2. dedupe the Reading List -----------------------------------------
    rl_by_url: dict[str, list[dict]] = {}
    for k in list(rl_children):
        if k.get("WebBookmarkType") == "WebBookmarkTypeLeaf" and k.get("URLString"):
            rl_by_url.setdefault(norm(k["URLString"]), []).append(k)
    rl_removed = 0
    for u, leaves in rl_by_url.items():
        if len(leaves) < 2:
            continue
        winner = next((l for l in leaves if not is_mojibake(title_of(l))), leaves[0])
        for dup in leaves:
            if dup is not winner:
                rl_children.remove(dup)
                rl_removed += 1
    print(f"Reading List duplicate items removed: {rl_removed}")

    # ---- 3. seeded default favourites (bar + cluster folders) ---------------
    # Captured from the ORIGINAL bar contents, before any mutation, so the
    # full Apple default set is known even after the bar is emptied.
    bar = next((ch for ch in top if ch.get("Title") == "BookmarksBar"), None)
    seeded_urls = {l.get("URLString") for l in (bar.get("Children", []) if bar else [])
                   if l.get("WebBookmarkType") == "WebBookmarkTypeLeaf"}
    bar_removed = cluster_removed = 0
    if seeded_urls and empty_bar:
        keep = [l for l in bar.get("Children", [])
                if not (l.get("WebBookmarkType") == "WebBookmarkTypeLeaf"
                        and l.get("URLString") in seeded_urls)]
        bar_removed = len(bar.get("Children", [])) - len(keep)
        bar["Children"] = keep

        def count_seeded(node: dict) -> int:
            n = 0
            for k in node.get("Children", []):
                if k.get("WebBookmarkType") == "WebBookmarkTypeLeaf" \
                        and k.get("URLString") in seeded_urls:
                    n += 1
                else:
                    n += count_seeded(k)
            return n

        def strip_seeded(node: dict) -> int:
            removed_here = 0
            for k in list(node.get("Children", [])):
                if k.get("WebBookmarkType") == "WebBookmarkTypeLeaf" \
                        and k.get("URLString") in seeded_urls:
                    node["Children"].remove(k)
                    removed_here += 1
                else:
                    removed_here += strip_seeded(k)
            return removed_here

        for ch in top:
            if ch is bar or ch.get("WebBookmarkType") != "WebBookmarkTypeList":
                continue
            if ch.get("Title") in NEVER_TOUCH or is_rl_folder(ch):
                continue
            n = count_seeded(ch)
            if n >= SEED_CLUSTER_MIN:
                cluster_removed += strip_seeded(ch)
                print(f"   stripped {n} seeded defaults from cluster folder {ch.get('Title')!r}")
        print(f"Seeded default favourites removed: bar {bar_removed}, cluster folders {cluster_removed}")

    # ---- 4. bookmark dedupe with folder priority -----------------------------
    parent_of, order = index_tree(root)
    groups: dict[str, list[tuple[dict, str | None]]] = {}
    for leaf, top_title in order:
        if top_title in NEVER_TOUCH or top_title == "com.apple.ReadingList":
            continue
        u = leaf.get("URLString", "")
        if u:
            groups.setdefault(norm(u), []).append((leaf, top_title))

    removed = pruned_folders = 0
    pairs: defaultdict[str, int] = defaultdict(int)
    samples: list[str] = []
    for u, entries in groups.items():
        if len(entries) < 2:
            continue
        def rank(e):
            leaf, t = e
            added = leaf.get("dateAdded")
            ts = added.timestamp() if isinstance(added, datetime) else 1e12
            return (folder_class(t), is_mojibake(title_of(leaf)), ts)
        entries.sort(key=rank)
        winner = entries[0]
        for leaf, t in entries[1:]:
            p = parent_of.get(id(leaf))
            if p is None:
                continue
            p["Children"].remove(leaf)
            removed += 1
            pairs[f"{winner[1] or '(root)'}  <-  {t or '(root)'}"] += 1
            if len(samples) < 8:
                samples.append(f"{title_of(leaf)[:40]!r}: {u[:48]}")
            pruned_folders_temp: list = []
            prune_emptied(p, parent_of, root, pruned_folders_temp)
            pruned_folders += len(pruned_folders_temp)

    print(f"\nBookmark duplicate leaves removed: {removed} (emptied subfolders pruned: {pruned_folders})")
    for k, n in sorted(pairs.items(), key=lambda x: -x[1]):
        print(f"   {n:>4}  kept [{k.split('  <-  ')[0]}], dropped copy in [{k.split('  <-  ')[1]}]")
    for s in samples:
        print(f"   e.g. {s}")

    # ---- 5. report ------------------------------------------------------------
    emptied = [ch.get("Title") for ch in top
               if ch.get("WebBookmarkType") == "WebBookmarkTypeList"
               and ch.get("Title") not in STRUCTURAL and not ch.get("Children")]
    if emptied:
        print(f"\nTop-level folders now empty (delete in Safari so it syncs): {emptied}")

    total_changed = len(strays) + rl_removed + removed + bar_removed + cluster_removed
    if total_changed == 0:
        print("\nNothing to do.")
        return 0
    if not apply:
        print("\nDRY RUN — nothing changed. Re-run with --apply.")
        return 0

    backup = f"{PLIST}.dedupe-{time.strftime('%Y%m%d-%H%M%S')}.bak"
    shutil.copy2(PLIST, backup)
    with open(PLIST, "wb") as f:
        plistlib.dump(root, f)
    print(f"\n✅ Applied. Backup → {os.path.basename(backup)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
