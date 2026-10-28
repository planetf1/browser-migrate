#!/usr/bin/env python3
"""
migrate.py — unified browser data migration on macOS.

Chromium ↔ Chromium ↔ Safari, per profile, in one interactive flow.

  History    Chromium SQLite ↔ Safari History.db (Cocoa conversion,
             generation-stamped so rows reach iCloud history sync)
  Bookmarks  Chromium JSON ↔ Safari shared Bookmarks.plist
  Autofill   Chromium ↔ Chromium only (Safari has no accessible store)

Work/personal roles are classified at run time (nothing hardcoded) and
routing insists work → work, personal → personal. Cross-role routing is
refused unless --allow-cross-role is given.

Dry run by default: the tool plans against read-only snapshots and writes
nothing until --apply. Sources are always read from snapshots and may stay
open; the target browser must be quit for --apply (checked automatically).

Usage:
  uv run migrate.py                     # plan only
  uv run migrate.py --apply             # write (with backups)
  uv run migrate.py --apply --allow-cross-role
"""

import argparse
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

from direct_import_safari import import_items
from safari_to_edge import merge_into_edge, read_safari_visits, snapshot
from safari_to_edge_bookmarks import collect_safari_leaves, pull_into_chromium
from sync_edge_bookmarks import run_sync

ALL_BROWSER_ROOTS: list[tuple[str, str]] = [
    ("Microsoft Edge",        os.path.expanduser("~/Library/Application Support/Microsoft Edge")),
    ("Microsoft Edge Dev",    os.path.expanduser("~/Library/Application Support/Microsoft Edge Dev")),
    ("Microsoft Edge Beta",   os.path.expanduser("~/Library/Application Support/Microsoft Edge Beta")),
    ("Microsoft Edge Canary", os.path.expanduser("~/Library/Application Support/Microsoft Edge Canary")),
    ("Google Chrome Dev",     os.path.expanduser("~/Library/Application Support/Google/Chrome Dev")),
    ("Google Chrome Beta",    os.path.expanduser("~/Library/Application Support/Google/Chrome Beta")),
    ("Google Chrome",         os.path.expanduser("~/Library/Application Support/Google/Chrome")),
    ("Google Chrome Canary",  os.path.expanduser("~/Library/Application Support/Google/Chrome Canary")),
    ("Chromium",              os.path.expanduser("~/Library/Application Support/Chromium")),
    ("Brave",                 os.path.expanduser("~/Library/Application Support/BraveSoftware/Brave-Browser")),
    ("Vivaldi",               os.path.expanduser("~/Library/Application Support/Vivaldi")),
]

# Bundle names for the target-quit guard (matched against the full .app
# path so embedded WebView helpers from other apps never false-positive).
BROWSER_BUNDLES = {
    "Microsoft Edge": "Microsoft Edge.app",
    "Microsoft Edge Dev": "Microsoft Edge Dev.app",
    "Microsoft Edge Beta": "Microsoft Edge Beta.app",
    "Microsoft Edge Canary": "Microsoft Edge Canary.app",
    "Google Chrome": "Google Chrome.app",
    "Google Chrome Dev": "Google Chrome Dev.app",
    "Google Chrome Beta": "Google Chrome Beta.app",
    "Google Chrome Canary": "Google Chrome Canary.app",
    "Chromium": "Chromium.app",
    "Brave": "Brave Browser.app",
    "Vivaldi": "Vivaldi.app",
}

SAFARI_DEFAULT_DIR = os.path.expanduser("~/Library/Safari")
SAFARI_PROFILES_BASE = os.path.expanduser(
    "~/Library/Containers/com.apple.Safari/Data/Library/Safari/Profiles")
SAFARI_PLIST = os.path.expanduser("~/Library/Safari/Bookmarks.plist")

# Chromium stores µs since 1601-01-01; Safari Cocoa seconds since 2001-01-01.
CHROMIUM_TO_COCOA = 11_644_473_600 + 978_307_200


def die(msg: str) -> None:
    print(f"\n❌ {msg}", file=sys.stderr)
    sys.exit(1)


def hdr(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 56 - len(title)))


# ── Profile detection ──────────────────────────────────────────────────────────

def _read_prefs(prefs_path: str, fallback: str) -> tuple[str, str]:
    try:
        with open(prefs_path, "r", encoding="utf-8") as f:
            prefs = json.load(f)
        name = prefs.get("profile", {}).get("name", fallback)
        acc = prefs.get("account_info", [])
        email = acc[0].get("email", "") if acc else ""
        return name, email
    except Exception:
        return fallback, ""


def get_chromium_profiles() -> list[dict]:
    profiles = []
    for browser_name, root in ALL_BROWSER_ROOTS:
        if not os.path.exists(root):
            continue
        for item in sorted(os.listdir(root)):
            if not item.startswith(("Default", "Profile")):
                continue
            full_path = os.path.join(root, item)
            history_path = os.path.join(full_path, "History")
            if not os.path.isdir(full_path) or not os.path.exists(history_path):
                continue
            name, email = _read_prefs(os.path.join(full_path, "Preferences"), item)
            suffix = f" [{item}]" if name != item else ""
            display = f"{browser_name} — {name}{suffix}"
            if email:
                display += f" ({email})"
            profiles.append({
                "kind": "chromium",
                "browser": browser_name,
                "email": email,
                "name": display,
                "history_path": history_path,
                "bookmarks_path": os.path.join(full_path, "Bookmarks"),
                "web_data_path": os.path.join(full_path, "Web Data"),
                "profile_dir": full_path,
            })
    return profiles


def get_safari_profiles() -> list[dict]:
    profiles = []
    if os.path.exists(os.path.join(SAFARI_DEFAULT_DIR, "History.db")):
        profiles.append({
            "kind": "safari",
            "browser": "Safari",
            "email": "",
            "name": "Safari — default (~/Library/Safari)",
            "history_path": os.path.join(SAFARI_DEFAULT_DIR, "History.db"),
            "bookmarks_path": SAFARI_PLIST,
            "web_data_path": None,
            "profile_dir": SAFARI_DEFAULT_DIR,
        })
    if os.path.isdir(SAFARI_PROFILES_BASE):
        for item in sorted(os.listdir(SAFARI_PROFILES_BASE)):
            full = os.path.join(SAFARI_PROFILES_BASE, item)
            if not os.path.exists(os.path.join(full, "History.db")):
                continue  # companion profiles without history
            profiles.append({
                "kind": "safari",
                "browser": "Safari",
                "email": "",
                "name": f"Safari — {item.split('-')[0]}",
                "history_path": os.path.join(full, "History.db"),
                "bookmarks_path": SAFARI_PLIST,
                "web_data_path": None,
                "profile_dir": full,
            })
    return profiles


# ── Role classification (runtime, nothing hardcoded) ──────────────────────────

def classify_roles(profiles: list[dict]) -> None:
    hdr("Profiles")
    for i, p in enumerate(profiles, 1):
        print(f"  [{i}] {p['name']}")
    hdr("Roles")
    print("Which profiles are WORK? Everything else is personal.")
    ans = input("Work profiles (numbers, or Enter for none): ").strip()
    work: set[int] = set()
    for tok in ans.replace(",", " ").split():
        try:
            work.add(int(tok))
        except ValueError:
            die(f"not a profile number: {tok!r}")
    for i, p in enumerate(profiles, 1):
        p["role"] = "work" if i in work else "personal"
    # Same email ⇒ same role, across browsers.
    work_emails = {p["email"] for p in profiles if p["role"] == "work" and p["email"]}
    for p in profiles:
        if p["email"] and p["email"] in work_emails:
            p["role"] = "work"
    for role in ("work", "personal"):
        names = [p["name"] for p in profiles if p["role"] == role]
        print(f"  {role:9} {', '.join(names) if names else '(none)'}")


# ── Selection UI ───────────────────────────────────────────────────────────────

def pick_sources(profiles: list[dict]) -> list[dict]:
    hdr("Source")
    for i, p in enumerate(profiles, 1):
        print(f"  [{i}] {p['name']}  ·  {p['role']}")
    ans = input("Profiles (numbers, or all): ").strip().lower()
    if ans == "all":
        return list(profiles)
    picked = []
    for tok in ans.replace(",", " ").split():
        try:
            idx = int(tok) - 1
        except ValueError:
            die(f"not a profile number: {tok!r}")
        if not 0 <= idx < len(profiles):
            die(f"no profile number {tok}")
        picked.append(profiles[idx])
    if not picked:
        die("no source selected")
    return picked


def pick_target(profiles: list[dict], sources: list[dict]) -> dict:
    """Individual profiles plus, where useful, role-routed group targets."""
    src_ids = {id(s) for s in sources}
    entries: list[dict] = [p for p in profiles if id(p) not in src_ids]

    # One group entry per browser when ≥2 targettable profiles remain, so a
    # single run can route both roles at once.
    by_browser: dict[str, list[dict]] = {}
    for p in profiles:
        if id(p) not in src_ids:
            by_browser.setdefault(p["browser"], []).append(p)
    for browser, ps in sorted(by_browser.items()):
        if len(ps) >= 2:
            entries.append({
                "kind": "group",
                "browser": browser,
                "name": f"{browser} — all profiles (routed by role)",
                "profiles": ps,
            })

    hdr("Target")
    for i, p in enumerate(entries, 1):
        label = p["name"] if p["kind"] == "group" else f"{p['name']}  ·  {p['role']}"
        print(f"  [{i}] {label}")
    ans = input("Target (number): ").strip()
    try:
        idx = int(ans) - 1
    except ValueError:
        die(f"not a target number: {ans!r}")
    if not 0 <= idx < len(entries):
        die(f"no target number {ans}")
    return entries[idx]


def resolve_routes(sources: list[dict], target: dict,
                   allow_cross: bool) -> list[tuple[dict, dict]]:
    """Pair each source with its target profile, insisting role-to-role."""
    if target["kind"] == "group":
        routes: list[tuple[dict, dict]] = []
        unrouted = []
        for s in sources:
            if any(s is t for t in target["profiles"]):
                continue  # self
            match = next((t for t in target["profiles"] if t["role"] == s["role"]), None)
            if match is None:
                unrouted.append(s)
            else:
                routes.append((s, match))
        if unrouted:
            roles = " + ".join(sorted({s["role"] for s in unrouted}))
            die(f"no {roles} profile in the target group for: "
                + ", ".join(s["name"] for s in unrouted)
                + ". Classify a matching target profile, or deselect these sources.")
        return routes

    conflicts = [s for s in sources if s["role"] != target["role"]]
    if conflicts:
        if not allow_cross:
            die("cross-role routing refused: "
                + ", ".join(s["name"] for s in conflicts)
                + f" would land in the {target['role']} target '{target['name']}'. "
                  "Pick the matching target, or pass --allow-cross-role.")
        print("\n  ⚠ cross-role routing allowed for: "
              + ", ".join(s["name"] for s in conflicts))
    return [(s, target) for s in sources]


# ── Quit guards and backups ────────────────────────────────────────────────────

def browser_running(pattern: str) -> bool:
    return subprocess.run(["pgrep", "-if", pattern],
                          capture_output=True).returncode == 0


def guard_targets(routes: list[tuple[dict, dict]], safari_plist_write: bool) -> None:
    """Every browser that will be written must be quit."""
    names = {t["browser"] for _, t in routes}
    if safari_plist_write:
        names.add("Safari")
    running = []
    for browser in sorted(names):
        if browser == "Safari":
            if subprocess.run(["pgrep", "-x", "Safari"]).returncode == 0:
                running.append("Safari")
        elif browser_running(BROWSER_BUNDLES.get(browser, browser + ".app")):
            running.append(browser)
    if running:
        die("target browser still running: " + ", ".join(running)
            + ". Quit it (⌘Q) and re-run with --apply.")


def _backup(path: str) -> None:
    """Timestamped backup including SQLite sidecar files."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    stem = f".migrator-{ts}"
    n = 1
    while os.path.exists(f"{path}{stem}.bak"):
        stem = f".migrator-{ts}-{n}"
        n += 1
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(path + ext):
            shutil.copy2(path + ext, f"{path}{ext}{stem}.bak")
    print(f"  Backed up → {os.path.basename(path)}{stem}.bak")


def _dst_copy(path: str, tmpdir: str, name: str) -> str:
    """Dry-run stand-in for a target file: a WAL-aware snapshot copy, or a
    fresh path when the target file does not exist yet."""
    dst = os.path.join(tmpdir, name)
    if os.path.exists(path):
        snapshot(path, tmpdir, name)
    return dst


# ── History ────────────────────────────────────────────────────────────────────

def read_history(history_path: str) -> tuple[list, list]:
    tmp = f"/tmp/hist_src_{os.getpid()}.sqlite"
    try:
        shutil.copy2(history_path, tmp)
        conn = sqlite3.connect(tmp)
        c = conn.cursor()
        c.execute(
            "SELECT url, title, visit_count, last_visit_time FROM urls"
            " WHERE url IS NOT NULL AND last_visit_time > 0"
        )
        urls = c.fetchall()
        c.execute(
            "SELECT u.url, v.visit_time, v.transition, v.visit_duration"
            " FROM visits v JOIN urls u ON v.url = u.id WHERE v.visit_time > 0"
        )
        visits = c.fetchall()
        conn.close()
        return urls, visits
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def merge_history(src_urls: list, src_visits: list, dst_path: str) -> tuple[int, int]:
    conn = sqlite3.connect(dst_path)
    conn.execute("PRAGMA journal_mode=WAL")
    c = conn.cursor()

    c.execute("SELECT id, url, visit_count, last_visit_time FROM urls")
    existing_urls: dict[str, dict] = {
        row[1]: {"id": row[0], "visit_count": row[2], "last_visit_time": row[3]}
        for row in c.fetchall()
    }
    c.execute("SELECT url, visit_time FROM visits")
    existing_visits: set[tuple[int, int]] = set(c.fetchall())

    url_id_map: dict[str, int] = {}
    urls_added = 0

    for url, title, visit_count, last_visit_time in src_urls:
        if url in existing_urls:
            rec = existing_urls[url]
            url_id_map[url] = rec["id"]
            c.execute(
                "UPDATE urls SET visit_count = ?, last_visit_time = ?,"
                " title = CASE WHEN title = '' THEN ? ELSE title END WHERE id = ?",
                (rec["visit_count"] + visit_count, max(rec["last_visit_time"], last_visit_time), title or "", rec["id"]),
            )
        else:
            c.execute(
                "INSERT INTO urls (url, title, visit_count, typed_count, last_visit_time, hidden)"
                " VALUES (?, ?, ?, 0, ?, 0)",
                (url, title or "", max(1, visit_count), last_visit_time),
            )
            new_id = c.lastrowid
            assert new_id is not None
            url_id_map[url] = new_id
            existing_urls[url] = {"id": new_id, "visit_count": visit_count, "last_visit_time": last_visit_time}
            urls_added += 1

    visits_added = 0
    for url_str, visit_time, transition, visit_duration in src_visits:
        cid = url_id_map.get(url_str)
        if cid is None:
            continue
        if (cid, visit_time) in existing_visits:
            continue
        c.execute(
            "INSERT INTO visits (url, visit_time, from_visit, transition, segment_id, visit_duration)"
            " VALUES (?, ?, 0, ?, 0, ?)",
            (cid, visit_time, transition, visit_duration),
        )
        existing_visits.add((cid, visit_time))
        visits_added += 1

    conn.commit()
    conn.close()
    return urls_added, visits_added


def chromium_items_for_safari(sources: list[dict]) -> list[tuple[str, str, float]]:
    """(url, title, cocoa_time) per URL, latest visit wins — the established
    single-visit-per-item forward shape (matches export/direct-import)."""
    best: dict[str, tuple[str, float]] = {}
    for s in sources:
        urls, _ = read_history(s["history_path"])
        for url, title, _count, last_visit_time in urls:
            cocoa = last_visit_time / 1_000_000 - CHROMIUM_TO_COCOA
            if url in best and best[url][1] >= cocoa:
                continue
            best[url] = (title or "", cocoa)
    return [(url, t, c) for url, (t, c) in best.items()]


def migrate_history(routes: list[tuple[dict, dict]], apply: bool,
                    tmpdir: str) -> list[tuple[str, int]]:
    """Run every history route; returns [(safari db, stamp)] for verification."""
    hdr("History")
    stamps: list[tuple[str, int]] = []
    by_target: dict[int, tuple[dict, list[dict]]] = {}
    for s, t in routes:
        by_target.setdefault(id(t), (t, []))[1].append(s)

    for t, srcs in by_target.values():
        print(f"\n  → {t['name']}")
        chrom_srcs = [s for s in srcs if s["kind"] == "chromium"]
        safari_srcs = [s for s in srcs if s["kind"] == "safari"]

        if t["kind"] == "safari":
            if safari_srcs:
                print("  (Safari → Safari history is a no-op; skipped)")
            if not chrom_srcs:
                continue
            items = chromium_items_for_safari(chrom_srcs)
            inserted, skipped, stamp = import_items(t["history_path"], items, apply)
            print(f"  History:  +{inserted:,} URLs  ({skipped:,} already present)"
                  f"{'' if apply else '  (dry run)'}")
            if stamp:
                stamps.append((t["history_path"], stamp))
        else:
            if chrom_srcs:
                all_urls: list = []
                all_visits: list = []
                for s in chrom_srcs:
                    urls, visits = read_history(s["history_path"])
                    all_urls.extend(urls)
                    all_visits.extend(visits)
                    print(f"  Read {len(urls):,} URLs, {len(visits):,} visits ← {s['name']}")
                if apply:
                    _backup(t["history_path"])
                    dst = t["history_path"]
                else:
                    dst = _dst_copy(t["history_path"], tmpdir, "hist-dst.sqlite")
                h_urls, h_visits = merge_history(all_urls, all_visits, dst)
                print(f"  History:  +{h_urls:,} URLs  +{h_visits:,} visits"
                      f"{'' if apply else '  (dry run)'}")
            for s in safari_srcs:
                visits = read_safari_visits(
                    snapshot(s["history_path"], tmpdir, f"safsrc-{id(s)}.db"))
                if apply:
                    _backup(t["history_path"])
                    dst = t["history_path"]
                else:
                    dst = _dst_copy(t["history_path"], tmpdir, f"hist-dst-{id(t)}.sqlite")
                n_urls, n_visits, _ins = merge_into_edge(dst, visits, apply)
                print(f"  History:  +{n_urls:,} URLs  +{n_visits:,} visits ← {s['name']}"
                      f"  (url + visit_time ±2 µs dedupe){'' if apply else '  (dry run)'}")
    return stamps


# ── Bookmarks ──────────────────────────────────────────────────────────────────

def _collect_bm_urls(node: dict, out: set[str]) -> None:
    if node.get("type") == "url" and node.get("url"):
        out.add(node["url"])
    for child in node.get("children", []):
        _collect_bm_urls(child, out)


def _find_max_bm_id(node: dict) -> int:
    try:
        val = int(node.get("id", 0))
    except (ValueError, TypeError):
        val = 0
    return max(val, *(_find_max_bm_id(c) for c in node.get("children", [])), 0)


def _merge_bm_folder(src: dict, dst: dict, existing: set[str], counter: list[int]) -> int:
    added = 0
    for child in src.get("children", []):
        if child.get("type") == "url":
            url = child.get("url", "")
            if url and url not in existing:
                counter[0] += 1
                dst.setdefault("children", []).append({
                    "date_added": child.get("date_added", "0"),
                    "date_last_used": "0",
                    "guid": str(uuid.uuid4()),
                    "id": str(counter[0]),
                    "name": child.get("name", ""),
                    "type": "url",
                    "url": url,
                })
                existing.add(url)
                added += 1
        elif child.get("type") == "folder":
            match = next(
                (c for c in dst.get("children", [])
                 if c.get("type") == "folder" and c.get("name") == child.get("name")),
                None,
            )
            if match is not None:
                added += _merge_bm_folder(child, match, existing, counter)
            else:
                # Build candidate folder; only attach if anything gets added
                counter[0] += 1
                now = str(int(time.time() * 1_000_000))
                candidate: dict = {
                    "children": [],
                    "date_added": now,
                    "date_last_used": "0",
                    "date_modified": now,
                    "guid": str(uuid.uuid4()),
                    "id": str(counter[0]),
                    "name": child.get("name", ""),
                    "type": "folder",
                }
                sub = _merge_bm_folder(child, candidate, existing, counter)
                if sub > 0:
                    dst.setdefault("children", []).append(candidate)
                    added += sub
    return added


def _empty_bookmarks() -> dict:
    def _folder(name: str, fid: str) -> dict:
        return {"children": [], "date_added": "0", "date_last_used": "0",
                "date_modified": "0", "guid": str(uuid.uuid4()), "id": fid,
                "name": name, "type": "folder"}
    return {
        "checksum": "",
        "roots": {
            "bookmark_bar": _folder("Bookmarks bar", "1"),
            "other": _folder("Other bookmarks", "2"),
            "synced": _folder("Mobile bookmarks", "3"),
        },
        "version": 1,
    }


def merge_bookmarks(src_path: str, dst_path: str) -> int:
    with open(src_path, "r", encoding="utf-8") as f:
        src = json.load(f)
    if not os.path.exists(dst_path):
        dst = _empty_bookmarks()
    else:
        with open(dst_path, "r", encoding="utf-8") as f:
            dst = json.load(f)

    existing: set[str] = set()
    max_id = 0
    for key in ("bookmark_bar", "other", "synced"):
        root = dst.get("roots", {}).get(key, {})
        _collect_bm_urls(root, existing)
        max_id = max(max_id, _find_max_bm_id(root))

    counter = [max_id]
    added = 0
    for key in ("bookmark_bar", "other", "synced"):
        src_root = src.get("roots", {}).get(key)
        dst_root = dst.get("roots", {}).get(key)
        if src_root and dst_root:
            added += _merge_bm_folder(src_root, dst_root, existing, counter)

    dst["checksum"] = ""  # Chrome recomputes on next load
    with open(dst_path, "w", encoding="utf-8") as f:
        json.dump(dst, f, indent=3, ensure_ascii=False)

    return added


def _unique(items: list[dict]) -> list[dict]:
    """Order-preserving dedup for profile dicts (unhashable, id-keyed)."""
    out: list[dict] = []
    seen: set[int] = set()
    for it in items:
        if id(it) not in seen:
            seen.add(id(it))
            out.append(it)
    return out


def migrate_bookmarks(routes: list[tuple[dict, dict]], apply: bool,
                      tmpdir: str) -> None:
    hdr("Bookmarks")
    safari_targets = [t for _, t in routes if t["kind"] == "safari"]
    chromium_routes = [(s, t) for s, t in routes
                       if s["kind"] == "chromium" and t["kind"] == "chromium"]

    # Chromium → Safari: curated filing into the shared plist
    # (personal → scheme folders, work → Work/ subtree).
    if safari_targets:
        chrom_srcs = [s for s, _ in routes if s["kind"] == "chromium"]
        if chrom_srcs:
            personal = [s["profile_dir"] for s in chrom_srcs if s["role"] == "personal"]
            work = [s["profile_dir"] for s in chrom_srcs if s["role"] == "work"]
            print("\n  Chromium → Safari shared plist (curated filing)")
            run_sync(personal, work, apply)
        else:
            print("\n  no Chromium sources; Safari bookmark filing skipped")

    # Safari → Chromium: reversible 'From Safari' inbox per target profile.
    if any(s["kind"] == "safari" for s, _ in routes):
        chrom_targets = _unique([t for _, t in routes if t["kind"] == "chromium"])
        if chrom_targets:
            plist_snap = os.path.join(tmpdir, "Bookmarks.plist")
            shutil.copy2(SAFARI_PLIST, plist_snap)
            try:
                with open(plist_snap, "rb") as f:
                    tree = plistlib.load(f)
            except plistlib.InvalidFileException:
                die("Bookmarks.plist snapshot failed to parse — is Safari mid-write? "
                    "Retry, or quit Safari first.")
            personal_leaves, work_leaves, _excluded = collect_safari_leaves(tree, False)
            has_work_target = any(t["role"] == "work" for t in chrom_targets)
            has_personal_target = any(t["role"] == "personal" for t in chrom_targets)
            for t in chrom_targets:
                leaves = work_leaves if t["role"] == "work" else personal_leaves
                if t["role"] == "personal" and work_leaves and not has_work_target:
                    print(f"\n  ⚠ {len(work_leaves):,} Safari bookmarks in the Work/ zone "
                          "have no work target in this run — skipped "
                          "(bookmarks never cross roles)")
                if t["role"] == "work" and personal_leaves and not has_personal_target:
                    print(f"\n  ⚠ {len(personal_leaves):,} Safari bookmarks outside Work/ "
                          "have no personal target in this run — skipped "
                          "(bookmarks never cross roles)")
                print(f"\n  Safari shared tree → {t['name']}")
                pull_into_chromium(leaves, t["bookmarks_path"], apply)

    # Chromium → Chromium: folder-merge into each target profile.
    if chromium_routes:
        for t in _unique([t for _, t in chromium_routes]):
            srcs = [s for s, tt in chromium_routes if tt is t]
            if apply:
                if os.path.exists(t["bookmarks_path"]):
                    _backup(t["bookmarks_path"])
                dst = t["bookmarks_path"]
            else:
                dst = _dst_copy(t["bookmarks_path"], tmpdir, f"bm-dst-{id(t)}.json")
            added = 0
            for s in srcs:
                if not os.path.exists(s["bookmarks_path"]):
                    continue
                if s["bookmarks_path"] == t["bookmarks_path"]:
                    continue
                added += merge_bookmarks(s["bookmarks_path"], dst)
            print(f"  {t['name']}:  +{added:,} bookmarks{'' if apply else '  (dry run)'}")


# ── Autofill ───────────────────────────────────────────────────────────────────

def merge_autofill(src_web_data: str, dst_web_data: str) -> int:
    tmp = f"/tmp/wd_src_{os.getpid()}.sqlite"
    try:
        shutil.copy2(src_web_data, tmp)
        conn = sqlite3.connect(tmp)
        rows = conn.execute(
            "SELECT name, value, value_lower, date_created, date_last_used, count FROM autofill"
        ).fetchall()
        conn.close()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    dst = sqlite3.connect(dst_web_data)
    dst.execute("PRAGMA journal_mode=WAL")
    c = dst.cursor()
    added = 0
    for name, value, value_lower, date_created, date_last_used, count in rows:
        existing = c.execute(
            "SELECT count FROM autofill WHERE name = ? AND value = ?", (name, value)
        ).fetchone()
        if existing is None:
            c.execute(
                "INSERT INTO autofill (name, value, value_lower, date_created, date_last_used, count)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (name, value, value_lower, date_created, date_last_used, count),
            )
            added += 1
        else:
            c.execute(
                "UPDATE autofill SET count = count + ?, date_last_used = MAX(date_last_used, ?)"
                " WHERE name = ? AND value = ?",
                (count, date_last_used, name, value),
            )
    dst.commit()
    dst.close()
    return added


def migrate_autofill(routes: list[tuple[dict, dict]], apply: bool,
                     tmpdir: str) -> None:
    hdr("Autofill")
    if any(s["kind"] == "safari" or t["kind"] == "safari" for s, t in routes):
        print("  skipped — Safari has no accessible autofill store")
        return
    for t in _unique([t for _, t in routes]):
        srcs = [s for s, tt in routes if tt is t]
        if not os.path.exists(t["web_data_path"]):
            print(f"  {t['name']}:  no Web Data store, skipped")
            continue
        if apply:
            _backup(t["web_data_path"])
            dst = t["web_data_path"]
        else:
            dst = _dst_copy(t["web_data_path"], tmpdir, f"wd-dst-{id(t)}.sqlite")
        added = 0
        for s in srcs:
            if not os.path.exists(s["web_data_path"]):
                continue
            if s["web_data_path"] == t["web_data_path"]:
                continue
            added += merge_autofill(s["web_data_path"], dst)
        if added:
            print(f"  {t['name']}:  +{added:,} form values{'' if apply else '  (dry run)'}")


# ── iCloud verification (Safari history) ──────────────────────────────────────

def read_generations(db_path: str, tmpdir: str) -> dict[str, str]:
    snap = snapshot(db_path, tmpdir,
                    f"verify-{os.path.basename(os.path.dirname(db_path))}.db")
    conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    try:
        return dict(conn.execute(
            "SELECT key, value FROM metadata WHERE key LIKE '%generation%'"))
    finally:
        conn.close()


def verify_icloud(stamps: list[tuple[str, int]], tmpdir: str) -> None:
    hdr("iCloud verification")
    for db, stamp in stamps:
        print(f"  {os.path.basename(os.path.dirname(db))}: rows stamped generation {stamp}")
    print("\n  Launch Safari now and open any page (one history modification")
    print("  schedules the upload cycle, ~90 s later).")
    ans = input("  Watch the upload watermark now? [y/N] ").strip().lower()
    if ans not in ("y", "yes"):
        print("  Later, check with:")
        for db, stamp in stamps:
            print(f'    sqlite3 {db} "select key,value from metadata')
            print(f'      where key like \'%generation%\';"   → last_synced must read {stamp}')
        return
    deadline = time.time() + 240
    while time.time() < deadline:
        time.sleep(10)
        all_ok = True
        for db, stamp in stamps:
            gens = read_generations(db, tmpdir)
            last = gens.get("last_synced_generation")
            if last is None or int(last) < stamp:
                all_ok = False
                print(f"  … waiting (last_synced={last}, want {stamp})")
            else:
                print(f"  ✓ {os.path.basename(os.path.dirname(db))}: "
                      f"last_synced={last} — rows uploaded to iCloud")
        if all_ok:
            print("\n  ✓ iCloud history upload verified.")
            return
    print("\n  ⚠ timed out — the rows are still inside the upload window and")
    print("    should upload on the next idle cycle. Re-check later with the")
    print("    sqlite3 command above; last_synced must land exactly on the stamp.")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Migrate browsing data between Chromium browsers and Safari")
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default: dry run)")
    ap.add_argument("--allow-cross-role", action="store_true",
                    help="permit work → personal or personal → work routing")
    a = ap.parse_args()

    print("── browser-migrate " + "─" * 42)
    print("History · bookmarks · autofill — Chromium ↔ Safari, per profile")
    print("Dry run by default; --apply to write.")

    profiles = get_chromium_profiles() + get_safari_profiles()
    if not profiles:
        die("no profiles found")

    classify_roles(profiles)
    sources = pick_sources(profiles)
    target = pick_target(profiles, sources)
    routes = resolve_routes(sources, target, a.allow_cross_role)
    if not routes:
        print("\nNothing to do (only self-routes selected).")
        return 0

    tmpdir = tempfile.mkdtemp(prefix="migrate-")
    try:
        if a.apply:
            safari_plist_write = bool(
                [t for _, t in routes if t["kind"] == "safari"]
                and any(s["kind"] == "chromium" for s, _ in routes))
            guard_targets(routes, safari_plist_write)

        stamps = migrate_history(routes, a.apply, tmpdir)
        migrate_bookmarks(routes, a.apply, tmpdir)
        migrate_autofill(routes, a.apply, tmpdir)

        if a.apply:
            print("\n── Done " + "─" * 52)
            print("Launch the target browser to verify.")
            if stamps:
                verify_icloud(stamps, tmpdir)
        else:
            print("\n── Dry run " + "─" * 48)
            print("Nothing written. Re-run with --apply to make these changes.")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
