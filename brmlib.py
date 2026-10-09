#!/usr/bin/env python3
"""
brmlib — shared bookmark-store model for browser-migrate tools.

One in-memory leaf model over four stores, with per-store adapters:

  safari-personal  shared Bookmarks.plist minus the Work/ subtree and the
                   excluded zones (Reading List, Edge Workspaces)
  safari-work      the Work/ subtree of the same plist (the only place the
                   work/personal boundary needs code enforcement — for Edge
                   the profile IS the file)
  edge-personal    Edge Beta 'Default' Bookmarks JSON
  edge-work        Edge Beta 'Profile 1' Bookmarks JSON

Reading is always done on snapshot copies (browsers may stay running).
Writing requires the owning browser to be quit and takes a timestamped
backup first. Dedupe groups by canonical URL (tracking params stripped,
host normalised, trailing slash dropped) but never rewrites the surviving
bookmark — losers are deleted, winners are untouched.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass, field

import plistlib

EDGE_ROOT = os.path.expanduser("~/Library/Application Support/Microsoft Edge Beta")
PLIST_PATH = os.path.expanduser("~/Library/Safari/Bookmarks.plist")

# Verified per run from Preferences; a mismatch aborts rather than writes
# to the wrong profile.
EDGE_DIRS = {
    "edge-personal": ("Default", "nigel@cherrybyte.me.uk"),
    "edge-work": ("Profile 1", "jonesn@uk.ibm.com"),
}

SAFARI_WORK_TITLE = "Work"
SAFARI_ZONE_EXCLUDE = {"Reading List", "com.apple.ReadingList", "Edge Workspaces"}

# Query params that never distinguish one page from another
STRIP_PARAMS = {
    "fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "mcp_token",
    "utm_reader", "utm_source", "utm_medium", "utm_campaign",
    "utm_term", "utm_content",
}


def die(msg: str) -> None:
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


# ── canonicalisation ───────────────────────────────────────────────────────────

def canonicalise(url: str) -> str:
    """Grouping key for dedupe. Never used to rewrite a stored URL."""
    url = url.strip()
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return url.lower()
    if p.scheme not in ("http", "https") or p.username:
        return url.lower()
    scheme = "https" if p.scheme == "http" else p.scheme
    host = (p.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    port = f":{p.port}" if p.port not in (None, 80, 443) else ""
    path = p.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = "&".join(
        kv for kv in p.query.split("&")
        if kv and kv.split("=", 1)[0].lower() not in STRIP_PARAMS
        and not kv.split("=", 1)[0].lower().startswith("utm_")
    ) if p.query else ""
    return urllib.parse.urlunsplit((scheme, host + port, path, query, ""))


# ── leaf model ─────────────────────────────────────────────────────────────────

@dataclass
class Leaf:
    title: str
    url: str
    path: list[str]
    node: dict                                  # the bookmark node itself
    owner: list                                  # the Children list holding it
    index: int                                   # encounter order (tie-break)


def _walk_plist(node: dict, path: list[str], owner: list, out: list[Leaf],
                counter: list[int]) -> None:
    kind = node.get("WebBookmarkType")
    if kind == "WebBookmarkTypeLeaf":
        url = node.get("URLString", "")
        if url:
            out.append(Leaf(node.get("URIDictionary", {}).get("title", "") or url,
                            url, list(path), node, owner, counter[0]))
            counter[0] += 1
    elif kind == "WebBookmarkTypeList":
        title = node.get("Title") or ""
        children = node.get("Children", [])
        for child in children:
            _walk_plist(child, path + ([title] if title else []), children,
                        out, counter)


def read_safari() -> tuple[dict, dict[str, list[Leaf]], str]:
    """Snapshot the shared plist; return (tree, zones, tmpdir)."""
    if not os.path.exists(PLIST_PATH):
        die(f"missing {PLIST_PATH}")
    tmpdir = tempfile.mkdtemp(prefix="brm-safari-")
    snap = os.path.join(tmpdir, "Bookmarks.plist")
    shutil.copy2(PLIST_PATH, snap)
    try:
        with open(snap, "rb") as f:
            tree = plistlib.load(f)
    except plistlib.InvalidFileException:
        die("Bookmarks.plist snapshot failed to parse — is Safari mid-write? "
            "Retry, or quit Safari first.")
    zones: dict[str, list[Leaf]] = {"safari-personal": [], "safari-work": []}
    for top in tree.get("Children", []):
        if top.get("WebBookmarkType") != "WebBookmarkTypeList":
            continue
        title = top.get("Title") or ""
        if title in SAFARI_ZONE_EXCLUDE:
            continue
        zone = "safari-work" if title == SAFARI_WORK_TITLE else "safari-personal"
        _walk_plist(top, [], top.get("Children", []), zones[zone], [0])
    return tree, zones, tmpdir


def _walk_json(node: dict, path: list[str], owner: list, out: list[Leaf],
               counter: list[int]) -> None:
    children = node.get("children", [])
    if node.get("type") == "url" and node.get("url"):
        out.append(Leaf(node.get("name", "") or node["url"], node["url"],
                        list(path), node, owner, counter[0]))
        counter[0] += 1
        return
    name = node.get("name") or ""
    for child in children:
        _walk_json(child, path + ([name] if name else []), children, out, counter)


def read_edge(store: str, tmpdir: str) -> tuple[dict, list[Leaf], str]:
    """Snapshot one Edge profile's Bookmarks JSON; (tree, leaves, path)."""
    edge_dir, expected_email = EDGE_DIRS[store]
    try:
        with open(os.path.join(EDGE_ROOT, edge_dir, "Preferences"),
                  "r", encoding="utf-8") as f:
            email = json.load(f).get("account_info", [{}])[0].get("email", "")
    except (OSError, json.JSONDecodeError):
        email = "(unknown)"
    if email != expected_email:
        die(f"Edge Beta {edge_dir!r} resolved to {email!r}, expected "
            f"{expected_email!r} — profile layout changed; refusing to "
            f"write. Update EDGE_DIRS in brmlib.")
    path = os.path.join(EDGE_ROOT, edge_dir, "Bookmarks")
    if not os.path.exists(path):
        die(f"missing {path}")
    snap = os.path.join(tmpdir, f"bookmarks-{store}.json")
    shutil.copy2(path, snap)
    with open(snap, "r", encoding="utf-8") as f:
        tree = json.load(f)
    leaves: list[Leaf] = []
    for root in tree.get("roots", {}).values():
        if isinstance(root, dict):
            _walk_json(root, [], root.get("children", []), leaves, [len(leaves)])
    return tree, leaves, path


# ── writing ────────────────────────────────────────────────────────────────────

def _browser_running(name: str, pattern: str) -> bool:
    return subprocess.run(["pgrep", "-if", pattern],
                          capture_output=True).returncode == 0


def write_safari(tree: dict, tmpdir: str) -> str:
    if _browser_running("Safari", "^Safari$|Safari$"):
        die("Safari is running — quit it (⌘Q) before writing the plist.")
    ts = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(PLIST_PATH, f"{PLIST_PATH}.dedupe-{ts}.bak")
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(tree, f, fmt=plistlib.FMT_XML)
    return f"Bookmarks.plist.dedupe-{ts}.bak"


def write_edge(path: str, tree: dict) -> str:
    if _browser_running("Edge", "Microsoft Edge Beta.app"):
        die("Edge is running — quit it (⌘Q) before writing Bookmarks JSON.")
    ts = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, f"{path}.dedupe-{ts}.bak")
    tree.pop("checksum", None)  # Chromium recomputes on next save
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tree, f, indent=3, ensure_ascii=False)
    return f"Bookmarks.dedupe-{ts}.bak"


def prune_empty_plist(node: dict, top: bool = False) -> bool:
    """Drop childless non-structural folders. Returns False if node died."""
    if node.get("WebBookmarkType") != "WebBookmarkTypeList":
        return True
    children = node.get("Children", [])
    node["Children"] = [c for c in children
                        if prune_empty_plist(c) or c.get("Title") in SAFARI_ZONE_EXCLUDE]
    if top:
        return True  # never delete top-level structural zones
    return bool(node.get("Children"))


def prune_empty_json(node: dict, top: bool = False) -> bool:
    if node.get("type") != "folder":
        return True
    node["children"] = [c for c in node.get("children", []) if prune_empty_json(c)]
    if top:
        return True  # roots stay
    return bool(node.get("children"))


# ── dedupe ─────────────────────────────────────────────────────────────────────

# Losers in these folders lose ties even against equally shallow folders
LOW_PRIORITY = {"", "misc", "unfiled", "from safari", "bookmarks bar", "other"}


def _rank(leaf: Leaf) -> int:
    last = leaf.path[-1].strip().lower() if leaf.path else ""
    return 0 if last in LOW_PRIORITY else 1


def dedupe_plan(leaves: list[Leaf]) -> list[tuple[str, Leaf, list[Leaf]]]:
    """[(canonical_url, winner, [losers])] for every duplicated canonical URL."""
    groups: dict[str, list[Leaf]] = defaultdict(list)
    for leaf in leaves:
        groups[canonicalise(leaf.url)].append(leaf)
    plan = []
    for key, group in sorted(groups.items()):
        if len(group) < 2:
            continue
        ranked = sorted(group, key=lambda l: (-_rank(l), -len(l.path), l.index))
        plan.append((key, ranked[0], ranked[1:]))
    return plan


def apply_removals(plan: list[tuple[str, Leaf, list[Leaf]]]) -> int:
    removed = 0
    for _, _, losers in plan:
        for loser in losers:
            if loser.node in loser.owner:
                loser.owner.remove(loser.node)
                removed += 1
    return removed
