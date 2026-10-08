#!/usr/bin/env python3
"""
reorganise_bookmarks.py — split Edge-imported bookmarks out of Safari's shared
Bookmarks.plist (~/Library/Safari/Bookmarks.plist). Reorganisation only:
nodes are reparented (and optionally renamed), never created or destroyed,
so UUIDs and sync state survive.

Two operations:
  1. Classify the Edge-import root folders ("Favourites Bar", "Other
     Favourites", "Mobile Favourites" — including Safari's " 2"/" 3" dedupe
     suffixes when both work and personal imports land) by CONTENT and move
     work-majority ones into the top-level "Work" folder.
  2. Sweep individual leaves (and folders whose URLs are all work) matching
     the work-domain list into "Work" as well.

Usage:
    uv run reorganise_bookmarks.py            # DRY RUN: prints planned moves
    uv run reorganise_bookmarks.py --apply    # performs them (backup first)

Safari MUST be quit for --apply.
"""

import os
import plistlib
import re
import shutil
import subprocess
import sys

PLIST = os.path.expanduser("~/Library/Safari/Bookmarks.plist")
BACKUP = PLIST + ".reorg.bak"

IMPORT_ROOT_RE = re.compile(
    r"^(Favourites Bar|Other Favourites|Mobile Favourites)( \d+)?$"
)

# Work-domain sweep list (matched as substring of URL host, lowercase).
WORK_DOMAINS = (
    "ibm.com", "ibmcloud.com", "yourlearning.ibm.com",
    "generative-computing", "egeria", "odpi",
    "login.microsoftonline.com", "teams.microsoft.com", "office.com",
    "sharepoint.com", "azure.com", "azuredevops.com",
    "uk.ibm.com", "w3.ibm.com", "bob.ibm.com", "research.ibm.com",
)


def die(msg: str) -> None:
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def host_of(url: str) -> str:
    parts = url.split("://", 1)
    if len(parts) < 2:
        return ""
    return parts[1].split("/", 1)[0].split(":", 1)[0].lower()


def is_work_url(url: str) -> bool:
    h = host_of(url)
    return any(d in h for d in WORK_DOMAINS)


def leaves_under(node: dict) -> list[dict]:
    if node.get("WebBookmarkType") == "WebBookmarkTypeLeaf":
        return [node]
    out = []
    for ch in node.get("Children", []):
        out.extend(leaves_under(ch))
    return out


def leaf_title(node: dict) -> str:
    # Leaves keep their title in URIDictionary, not 'Title'.
    return (node.get("URIDictionary") or {}).get("title", "") or node.get("Title", "")


def norm_url(url: str) -> str:
    return url.rstrip("/").lower()


def load_work_set(path: str) -> set[str]:
    """URLs from an Edge bookmark export (Netscape HTML)."""
    import re as _re
    text = open(path, encoding="utf-8").read()
    urls = _re.findall(r'<A HREF="([^"]+)"', text)
    return {norm_url(u) for u in urls}


def pull_by(node: dict, pred, work_children: list, moves: list, pruned: list, label: str) -> None:
    """Recursively under node: move whole child folders whose leaves all match
    `pred`; pull out individual matching leaves; prune folders that empty out.
    Top-level empties are left in place (reported, not destroyed)."""
    kids = node.get("Children", [])
    for k in list(kids):
        if k.get("WebBookmarkType") == "WebBookmarkTypeLeaf":
            if pred(k.get("URLString", "")):
                kids.remove(k)
                work_children.append(k)
                moves.append((k, 1, f"leaf {leaf_title(k)[:45]!r} ({label})"))
        elif k.get("Children"):
            urls = [l.get("URLString", "") for l in leaves_under(k)]
            if urls and all(pred(u) for u in urls):
                kids.remove(k)
                work_children.append(k)
                moves.append((k, len(urls), f"folder {k.get('Title', '')[:40]!r} (all {label}, {len(urls)})"))
            else:
                pull_by(k, pred, work_children, moves, pruned, label)
                if not k.get("Children"):
                    kids.remove(k)
                    pruned.append(k)


def is_mojibake(s: str) -> bool:
    # Classic UTF-8-read-as-Latin-1 corruption markers.
    return "â€" in s or "Â" in s


def dedupe_pass(root: dict) -> tuple[int, int, list[str]]:
    """Remove duplicate leaves (same URL), keeping the first non-mojibake
    occurrence. Prunes folders that empty out. Returns (removed, pruned, samples)."""
    # parent map + document order
    parent_of: dict[int, dict] = {}
    order: list[dict] = []

    def index(node: dict):
        for k in node.get("Children", []):
            parent_of[id(k)] = node
            if k.get("WebBookmarkType") == "WebBookmarkTypeLeaf":
                order.append(k)
            else:
                index(k)

    index(root)

    groups: dict[str, list[dict]] = {}
    for leaf in order:
        u = norm_url(leaf.get("URLString", ""))
        if u:
            groups.setdefault(u, []).append(leaf)

    removed = pruned = 0
    samples: list[str] = []
    for u, leaves in groups.items():
        if len(leaves) < 2:
            continue
        winner = next((l for l in leaves if not is_mojibake(leaf_title(l))), leaves[0])
        for dup in leaves:
            if dup is winner:
                continue
            p = parent_of.get(id(dup))
            if p is None:
                continue
            p["Children"].remove(dup)
            removed += 1
            if len(samples) < 8:
                samples.append(f"{leaf_title(dup)[:50]!r} = {u[:40]}")
            # prune emptied ancestors up to (not including) the top level
            anc = p
            while anc is not root and not anc.get("Children"):
                gp = parent_of.get(id(anc))
                if gp is None:
                    break
                gp["Children"].remove(anc)
                pruned += 1
                anc = gp
    return removed, pruned, samples


def main() -> int:
    apply = "--apply" in sys.argv
    do_dedupe = "--dedupe" in sys.argv
    ws_arg = None
    if "--work-set" in sys.argv:
        i = sys.argv.index("--work-set")
        if i + 1 >= len(sys.argv):
            die("--work-set needs a file argument (Edge bookmark HTML export)")
        ws_arg = sys.argv[i + 1]
    if apply and subprocess.run(["pgrep", "-x", "Safari"]).returncode == 0:
        die("Safari is running — quit it (⌘Q) first, then retry with --apply.")

    with open(PLIST, "rb") as f:
        root = plistlib.load(f)

    if do_dedupe:
        removed, pruned, samples = dedupe_pass(root)
        print(f"URL-duplicate leaves to remove: {removed} (emptied folders pruned: {pruned})")
        for s in samples:
            print(f"   e.g. drop {s}")
        if removed == 0:
            print("Nothing to dedupe.")
            return 0
        if not apply:
            print("\nDRY RUN — nothing changed. Re-run with --apply to remove duplicates.")
            return 0
        shutil.copy2(PLIST, BACKUP)
        with open(PLIST, "wb") as f:
            plistlib.dump(root, f)
        print(f"\n✅ Removed {removed} duplicate leaves. Backup → {os.path.basename(BACKUP)}")
        return 0

    top = root.get("Children", [])

    work = None
    for ch in top:
        if ch.get("WebBookmarkType") == "WebBookmarkTypeList" and ch.get("Title") == "Work":
            work = ch
            break
    if work is None:
        die("no top-level 'Work' folder found — create it in Safari first.")
    work_children = work.setdefault("Children", [])
    work_start_count = len(work_children)
    in_work = {id(c) for c in work_children}

    work_set = load_work_set(ws_arg) if ws_arg else set()
    if work_set:
        print(f"Work set: {len(work_set)} URLs from {os.path.basename(ws_arg)}")
    moves: list[tuple[dict, int, str]] = []  # node, n, desc

    def take(node: dict, n: int, desc: str, from_list: list) -> None:
        from_list.remove(node)
        work_children.append(node)
        moves.append((node, n, desc))

    # 1) whole Edge-import roots, classified by content
    for ch in list(top):
        if ch.get("WebBookmarkType") != "WebBookmarkTypeList":
            continue
        title = ch.get("Title", "")
        if not IMPORT_ROOT_RE.match(title) or id(ch) in in_work:
            continue
        urls = [l.get("URLString", "") for l in leaves_under(ch)]
        if not urls:
            continue
        work_n = sum(1 for u in urls if is_work_url(u))
        ratio = work_n / len(urls)
        if ratio >= 0.5:
            take(ch, len(urls),
                 f"folder {title!r} → Work ({work_n}/{len(urls)} work URLs, {ratio:.0%})",
                 top)
        else:
            print(f"   kept {title!r} in place ({work_n}/{len(urls)} work URLs — personal-majority)")

    # 2) precise work-set pass: pull exactly the work-profile bookmarks (any depth)
    pruned: list[dict] = []
    if work_set:
        before = len(moves)
        for ch in list(top):
            if ch.get("WebBookmarkType") != "WebBookmarkTypeList" or ch is work or id(ch) in in_work:
                continue
            pull_by(ch, lambda u, ws=work_set: norm_url(u) in ws,
                    work_children, moves, pruned, "work set")
        if len(moves) > before:
            print(f"   work-set pass: moved {len(moves) - before} nodes, pruned {len(pruned)} emptied folders")

    # 3) work-domain sweep at any depth — catches older work bookmarks
    #    (ibm.com, egeria, …) mixed into personal folders
    before = len(moves)
    for ch in list(top):
        if ch.get("WebBookmarkType") != "WebBookmarkTypeList" or ch is work or id(ch) in in_work:
            continue
        pull_by(ch, is_work_url, work_children, moves, pruned, "work domain")
    if len(moves) > before:
        print(f"   domain pass: moved {len(moves) - before} nodes, pruned {len(pruned) - (0 if not work_set else 0)} folders total")

    print(f"Target: top-level 'Work' folder (starts with {work_start_count} children)")
    if not moves:
        print("Nothing to move — 'Work' already holds everything matching.")
        return 0
    print(f"\nPlanned moves ({len(moves)}):")
    for _, n, desc in moves:
        print(f"   {n:>5}  {desc}")

    emptied = [ch.get("Title") for ch in top
               if ch is not work and ch.get("WebBookmarkType") == "WebBookmarkTypeList"
               and not ch.get("Children")]
    if emptied:
        print(f"\nTop-level folders left empty (delete in Safari if you like): {emptied}")

    if not apply:
        print("\nDRY RUN — nothing changed. Re-run with --apply to perform the moves.")
        return 0

    shutil.copy2(PLIST, BACKUP)
    with open(PLIST, "wb") as f:
        plistlib.dump(root, f)
    print(f"\n✅ Applied {len(moves)} move(s). Backup → {os.path.basename(BACKUP)}")
    print("Launch Safari and check the Work folder; restore the backup if anything looks wrong.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
