#!/usr/bin/env python3
"""
sync_edge_bookmarks.py — add Edge Beta bookmarks that Safari's shared
Bookmarks.plist lacks, plus Edge Workspaces as saved tab-set folders.

- personal-profile additions → scheme folders, using the classify/junk
  rules from categorise_bookmarks.py (same filing as the Sept cleanup)
- work-profile additions → the Work subtree, preserving Edge folder paths
  (root container names 'Favourites bar'/'Other Favourites' are stripped;
  'Mobile Favourites' is kept as a real folder)
- workspaces_v2 roots → top-level 'Edge Workspaces' folder (empty
  workspaces skipped). These are saved tab sets, deliberately NOT deduped
  against ordinary bookmarks.

Idempotent: any URL already present anywhere in the plist (Reading List
and Edge Workspaces included) is skipped, so re-runs are no-ops.

Usage:
    uv run sync_edge_bookmarks.py --personal <edge-dir> --work <edge-dir> [--apply]

Safari MUST be quit for --apply.
"""

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from urllib.parse import urlparse

import categorise_bookmarks as cb

PLIST = os.path.expanduser("~/Library/Safari/Bookmarks.plist")
WORKSPACE_ROOT = "Edge Workspaces"
# Edge root containers that are roots, not user folders (stripped from paths)
STRIP_ROOTS = {"Favourites bar", "Other Favourites"}


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


def make_leaf(url: str, title: str) -> dict:
    return {
        "WebBookmarkType": "WebBookmarkTypeLeaf",
        "WebBookmarkUUID": uuid.uuid4().hex.upper(),
        "URLString": url,
        "URIDictionary": {"title": title or url},
        "dateAdded": datetime.now(),
    }


def walk_edge(node: dict, path: list[str], out: list) -> None:
    """Collect (url, title, folder-path) for every url node, depth-first."""
    if node.get("type") == "url":
        if node.get("url"):
            out.append((node["url"], node.get("name") or "", list(path)))
        return
    name = node.get("name") or ""
    for c in node.get("children", []):
        walk_edge(c, path + [name], out)


def edge_bookmarks(profile_dir: str) -> dict[str, list]:
    """roots → [(url, title, path-below-root)] for bookmark_bar/other/synced."""
    with open(os.path.join(profile_dir, "Bookmarks"), encoding="utf-8") as f:
        data = json.load(f)
    roots: dict[str, list] = {}
    for key in ("bookmark_bar", "other", "synced"):
        root = data.get("roots", {}).get(key)
        items: list = []
        if root:
            walk_edge(root, [], items)
        roots[key] = items
    return roots


def edge_workspaces(profile_dir: str) -> list[tuple[str, list]]:
    """[(workspace name, [(url, title)])] for non-empty workspaces."""
    with open(os.path.join(profile_dir, "Bookmarks"), encoding="utf-8") as f:
        data = json.load(f)
    ws_root = data.get("roots", {}).get("workspaces_v2", {})
    out = []
    for ws in ws_root.get("children", []):
        items: list = []
        walk_edge(ws, [], items)
        if items:
            out.append((ws.get("name") or "Workspace", items))
    return out


def top_level(root: dict) -> list[dict]:
    return root.setdefault("Children", [])


def ensure_folder(children: list[dict], title: str) -> dict:
    for ch in children:
        if ch.get("WebBookmarkType") == "WebBookmarkTypeList" and ch.get("Title") == title:
            return ch
    node = {
        "WebBookmarkType": "WebBookmarkTypeList",
        "WebBookmarkUUID": uuid.uuid4().hex.upper(),
        "Title": title,
        "Children": [],
    }
    children.append(node)
    return node


def ensure_path(children: list[dict], path: list[str]) -> list[dict]:
    """Descend (creating as needed) through `path`; return the deepest Children."""
    for name in path:
        node = ensure_folder(children, name)
        children = node.setdefault("Children", [])
    return children


def safari_urls(root: dict) -> set[str]:
    urls: set[str] = set()

    def walk(node: dict) -> None:
        if node.get("WebBookmarkType") == "WebBookmarkTypeLeaf":
            if node.get("URLString"):
                urls.add(norm(node["URLString"]))
        else:
            for c in node.get("Children", []):
                walk(c)

    for ch in top_level(root):
        walk(ch)
    return urls


def run_sync(personal_dirs: list[str], work_dirs: list[str], apply: bool) -> int:
    """Delta-sync Chromium bookmarks into Safari's shared Bookmarks.plist.

    personal_dirs → scheme folders (categorise_bookmarks rules, junk-filtered);
    work_dirs     → the Work/ subtree, Edge folder paths preserved;
    workspaces_v2 roots (any dir) → top-level 'Edge Workspaces' folder.
    Idempotent: URLs already anywhere in the plist are skipped.
    Shared core of the CLI and migrate.py; returns 0 on success.
    """
    if apply and subprocess.run(["pgrep", "-x", "Safari"]).returncode == 0:
        die("Safari is running — quit it (⌘Q) first, then retry with --apply.")

    with open(PLIST, "rb") as f:
        root = plistlib.load(f)
    have = safari_urls(root)

    work_node = None
    if work_dirs:
        for ch in top_level(root):
            if ch.get("Title") == "Work" and ch.get("WebBookmarkType") == "WebBookmarkTypeList":
                work_node = ch
                break
        if work_node is None:
            die("no top-level 'Work' folder — create it in Safari first.")
        work_children = work_node.setdefault("Children", [])

    # --- personal additions → scheme folders --------------------------------
    added_personal: dict[str, list] = {}
    junk: list[tuple[str, str]] = []
    for personal_dir in personal_dirs:
        for root_key, items in edge_bookmarks(personal_dir).items():
            for url, title, path in items:
                if norm(url) in have:
                    continue
                why = cb.is_junk(url, title)
                if why:
                    junk.append((url, why))
                    continue
                folder = cb.classify(url, title, " > ".join([root_key] + path))
                added_personal.setdefault(folder, []).append((url, title))
                have.add(norm(url))

    # --- work additions → Work subtree (Edge folder paths preserved) ---------
    added_work: dict[str, list] = {}
    for work_dir in work_dirs:
        for root_key, items in edge_bookmarks(work_dir).items():
            for url, title, path in items:
                if norm(url) in have:
                    continue
                rel = path if root_key == "synced" else [p for p in path if p not in STRIP_ROOTS]
                rel = [p for p in rel if p]  # drop the roots' own empty name
                key = "/".join(rel) or "(top of Work)"
                added_work.setdefault(key, []).append((rel, url, title))
                have.add(norm(url))

    # --- workspaces → Edge Workspaces folder --------------------------------
    # Deduped against URLs already under the Edge Workspaces root (re-runs
    # are no-ops) but deliberately NOT against ordinary bookmark zones:
    # a workspace tab can also be a regular bookmark.
    ws_existing: set[str] = set()
    for ch in top_level(root):
        if ch.get("Title") == WORKSPACE_ROOT:
            def _walk_ws(node: dict) -> None:
                if node.get("WebBookmarkType") == "WebBookmarkTypeLeaf":
                    if node.get("URLString"):
                        ws_existing.add(norm(node["URLString"]))
                else:
                    for c in node.get("Children", []):
                        _walk_ws(c)
            _walk_ws(ch)
    workspaces: list[tuple[str, list]] = []
    for profile in personal_dirs + work_dirs:
        for name, items in edge_workspaces(profile):
            fresh = [(u, t) for u, t, _ in items if norm(u) not in ws_existing]
            for u, _ in fresh:
                ws_existing.add(norm(u))
            if fresh:
                workspaces.append((name, fresh))

    # --- report --------------------------------------------------------------
    n_p = sum(len(v) for v in added_personal.values())
    n_w = sum(len(v) for v in added_work.values())
    n_ws = sum(len(items) for _, items in workspaces)
    print(f"Personal additions: {n_p} (junk skipped: {len(junk)})")
    for folder, items in sorted(added_personal.items()):
        print(f"   {folder:18} {len(items):>3}  e.g. {items[0][1][:48]!r}")
    if junk:
        print(f"   junk skipped: {', '.join(u[:40] for u, _ in junk[:5])}")
    print(f"Work additions: {n_w}")
    for key, items in sorted(added_work.items()):
        print(f"   {key:32} {len(items):>3}  e.g. {items[0][2][:48]!r}")
    print(f"Workspaces: {n_ws} urls in {len(workspaces)} workspaces -> {WORKSPACE_ROOT}/")
    for name, items in workspaces:
        print(f"   {name:18} {len(items):>3}")

    if n_p == n_w == n_ws == 0:
        print("\nNothing to add.")
        return 0
    if not apply:
        print("\nDRY RUN — nothing changed. Re-run with --apply.")
        return 0

    # --- apply ---------------------------------------------------------------
    for folder, items in added_personal.items():
        children = ensure_path(top_level(root), [folder])
        for url, title in items:
            children.append(make_leaf(url, title))
    for key, items in added_work.items():
        for rel, url, title in items:
            children = ensure_path(work_children, rel)
            children.append(make_leaf(url, title))
    if workspaces:
        ws_children = ensure_path(top_level(root), [WORKSPACE_ROOT])
        for name, items in workspaces:
            ws_folder_children = ensure_path(ws_children, [name])
            for url, title in items:
                ws_folder_children.append(make_leaf(url, title))

    backup = f"{PLIST}.sync-{time.strftime('%Y%m%d-%H%M%S')}.bak"
    shutil.copy2(PLIST, backup)
    with open(PLIST, "wb") as f:
        plistlib.dump(root, f)
    print(f"\n✅ Applied. Backup → {os.path.basename(backup)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--personal", required=True, help="Edge profile dir (personal)")
    ap.add_argument("--work", required=True, help="Edge profile dir (work)")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    return run_sync([a.personal], [a.work], a.apply)


if __name__ == "__main__":
    sys.exit(main())
