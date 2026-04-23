#!/usr/bin/env python3
"""
Migrate browsing data between any Chromium-based browsers on macOS.
Works in any direction: Edge ↔ Chrome, Chrome ↔ Brave, etc.

Migrates (all with deduplication):
  - History    (SQLite History DB — URLs + individual visits)
  - Bookmarks  (JSON Bookmarks file — folder structure preserved)
  - Autofill   (SQLite Web Data — form field values only)

Not migrated (encrypted with browser-specific OS keychain keys):
  - Saved passwords  (Login Data)
  - Credit cards     (Web Data)

The target browser MUST be fully closed before running.
"""

import json
import os
import shutil
import sqlite3
import sys
import time
import uuid

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


def get_all_profiles() -> list[dict]:
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
            display = f"{browser_name} - {name}{suffix}"
            if email:
                display += f" ({email})"
            profiles.append({
                "name": display,
                "history_path": history_path,
                "bookmarks_path": os.path.join(full_path, "Bookmarks"),
                "web_data_path": os.path.join(full_path, "Web Data"),
            })
    return profiles


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


# ── UI helpers ─────────────────────────────────────────────────────────────────

def pick(label: str, items: list, allow_all: bool = True) -> list | None:
    if not items:
        print(f"No {label} found.")
        return None
    print(f"\n{label}:")
    for i, p in enumerate(items, 1):
        print(f"  [{i}] {p['name']}")
    prompt = f"\nSelect number{', or  all' if allow_all else ''}: "
    sel = input(prompt).strip().lower()
    if allow_all and sel == "all":
        return items
    try:
        idx = int(sel) - 1
        if 0 <= idx < len(items):
            return [items[idx]]
    except ValueError:
        pass
    print("Invalid selection.")
    return None


def _backup(path: str) -> None:
    shutil.copy2(path, path + ".migrator.bak")
    print(f"  Backed up → {os.path.basename(path)}.migrator.bak")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    print("--- Chromium Browser Data Migrator ---")
    print("Migrates: History · Bookmarks · Autofill form values\n")

    profiles = get_all_profiles()
    if not profiles:
        print("No profiles found.")
        return 1

    sources = pick("Source profiles", profiles, allow_all=True)
    if not sources:
        return 1

    src_paths = {s["history_path"] for s in sources}
    remaining = [p for p in profiles if p["history_path"] not in src_paths]
    targets = pick("Target profiles", remaining, allow_all=False)
    if not targets:
        return 1

    target = targets[0]
    print(f"\n⚠  Make sure {target['name']} is fully closed.")
    input("Press Enter when ready...")

    # ── History ────────────────────────────────────────────────────────────────
    print("\nReading source history...")
    all_urls: list = []
    all_visits: list = []
    for src in sources:
        urls, visits = read_history(src["history_path"])
        all_urls.extend(urls)
        all_visits.extend(visits)
        print(f"  {len(urls):>8,} URLs  {len(visits):>10,} visits  ← {src['name']}")

    print(f"\n── {target['name']} ──")
    _backup(target["history_path"])
    h_urls, h_visits = merge_history(all_urls, all_visits, target["history_path"])
    print(f"  History:   +{h_urls:,} URLs  +{h_visits:,} visits")

    # ── Bookmarks ──────────────────────────────────────────────────────────────
    bm_added = 0
    bm_backed_up = False
    for src in sources:
        if not os.path.exists(src["bookmarks_path"]):
            continue
        if src["bookmarks_path"] == target["bookmarks_path"]:
            continue
        if not bm_backed_up:
            if os.path.exists(target["bookmarks_path"]):
                _backup(target["bookmarks_path"])
            bm_backed_up = True
        bm_added += merge_bookmarks(src["bookmarks_path"], target["bookmarks_path"])
    if bm_added:
        print(f"  Bookmarks: +{bm_added:,}")

    # ── Autofill ───────────────────────────────────────────────────────────────
    af_added = 0
    af_backed_up = False
    for src in sources:
        if not os.path.exists(src["web_data_path"]):
            continue
        if not os.path.exists(target["web_data_path"]):
            continue
        if src["web_data_path"] == target["web_data_path"]:
            continue
        if not af_backed_up:
            _backup(target["web_data_path"])
            af_backed_up = True
        af_added += merge_autofill(src["web_data_path"], target["web_data_path"])
    if af_added:
        print(f"  Autofill:  +{af_added:,} form values")

    print("\nDone. Launch the target browser to verify.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
