#!/usr/bin/env python3
"""
Import Microsoft Edge browsing history into a Chromium-based browser.

Both Edge and Chrome use the same Chromium SQLite history schema and the same
epoch (microseconds since 1601-01-01), so no timestamp conversion is needed.

The target browser MUST be fully closed before running this script.
"""

import json
import os
import shutil
import sqlite3
import sys

EDGE_ROOTS = [
    os.path.expanduser("~/Library/Application Support/Microsoft Edge"),
    os.path.expanduser("~/Library/Application Support/Microsoft Edge Dev"),
    os.path.expanduser("~/Library/Application Support/Microsoft Edge Beta"),
    os.path.expanduser("~/Library/Application Support/Microsoft Edge Canary"),
]

CHROME_ROOTS = [
    ("Google Chrome Dev",  os.path.expanduser("~/Library/Application Support/Google/Chrome Dev")),
    ("Google Chrome Beta", os.path.expanduser("~/Library/Application Support/Google/Chrome Beta")),
    ("Google Chrome",      os.path.expanduser("~/Library/Application Support/Google/Chrome")),
    ("Google Chrome Canary", os.path.expanduser("~/Library/Application Support/Google/Chrome Canary")),
    ("Chromium",           os.path.expanduser("~/Library/Application Support/Chromium")),
    ("Brave",              os.path.expanduser("~/Library/Application Support/BraveSoftware/Brave-Browser")),
    ("Vivaldi",            os.path.expanduser("~/Library/Application Support/Vivaldi")),
]


def _read_prefs_display_name(prefs_path: str, fallback: str) -> tuple[str, str]:
    """Returns (profile_name, email) from a Preferences file."""
    try:
        with open(prefs_path, "r", encoding="utf-8") as f:
            prefs = json.load(f)
        name = prefs.get("profile", {}).get("name", fallback)
        acc = prefs.get("account_info", [])
        email = acc[0].get("email", "") if acc else ""
        return name, email
    except Exception:
        return fallback, ""


def get_edge_profiles() -> list[dict]:
    profiles = []
    for root in EDGE_ROOTS:
        if not os.path.exists(root):
            continue
        channel = os.path.basename(root).replace("Microsoft Edge", "").strip() or "Stable"
        for item in sorted(os.listdir(root)):
            full_path = os.path.join(root, item)
            history_path = os.path.join(full_path, "History")
            if not os.path.isdir(full_path) or not os.path.exists(history_path):
                continue
            name, email = _read_prefs_display_name(os.path.join(full_path, "Preferences"), item)
            display = f"Edge {channel} - {name}"
            if email:
                display += f" ({email})"
            profiles.append({"name": display, "history_path": history_path})
    return profiles


def get_chrome_profiles() -> list[dict]:
    profiles = []
    for browser_name, root in CHROME_ROOTS:
        if not os.path.exists(root):
            continue
        for item in sorted(os.listdir(root)):
            if not item.startswith(("Default", "Profile")):
                continue
            full_path = os.path.join(root, item)
            history_path = os.path.join(full_path, "History")
            if not os.path.isdir(full_path) or not os.path.exists(history_path):
                continue
            name, _ = _read_prefs_display_name(os.path.join(full_path, "Preferences"), item)
            profiles.append({"name": f"{browser_name} - {name}", "history_path": history_path})
    return profiles


def read_edge_history(history_path: str) -> tuple[list, list]:
    """
    Returns (urls, visits):
      urls:   [(url, title, visit_count, last_visit_time), ...]
      visits: [(url_str, visit_time, transition, visit_duration), ...]
    """
    temp_db = f"/tmp/edge_src_{os.getpid()}.sqlite"
    try:
        shutil.copy2(history_path, temp_db)
        conn = sqlite3.connect(temp_db)
        c = conn.cursor()

        c.execute(
            "SELECT url, title, visit_count, last_visit_time FROM urls"
            " WHERE url IS NOT NULL AND last_visit_time > 0"
        )
        urls = c.fetchall()

        c.execute(
            "SELECT u.url, v.visit_time, v.transition, v.visit_duration"
            " FROM visits v JOIN urls u ON v.url = u.id"
            " WHERE v.visit_time > 0"
        )
        visits = c.fetchall()

        conn.close()
        return urls, visits
    finally:
        if os.path.exists(temp_db):
            os.remove(temp_db)


def merge_into_chrome(
    edge_urls: list, edge_visits: list, chrome_history_path: str
) -> tuple[int, int]:
    """
    Merges edge_urls and edge_visits into the Chrome History DB in-place.
    - New URLs are inserted.
    - Existing URLs get their visit_count incremented and last_visit_time updated.
    - Visits are deduplicated by (chrome_url_id, visit_time).
    Returns (urls_added, visits_added).
    """
    conn = sqlite3.connect(chrome_history_path)
    conn.execute("PRAGMA journal_mode=WAL")
    c = conn.cursor()

    # Load existing URL map: url_string → {id, visit_count, last_visit_time}
    c.execute("SELECT id, url, visit_count, last_visit_time FROM urls")
    existing_urls: dict[str, dict] = {
        row[1]: {"id": row[0], "visit_count": row[2], "last_visit_time": row[3]}
        for row in c.fetchall()
    }

    # Load existing visit keys for deduplication
    c.execute("SELECT url, visit_time FROM visits")
    existing_visits: set[tuple[int, int]] = set(c.fetchall())

    url_id_map: dict[str, int] = {}
    urls_added = 0

    for url, title, visit_count, last_visit_time in edge_urls:
        if url in existing_urls:
            rec = existing_urls[url]
            url_id_map[url] = rec["id"]
            new_count = rec["visit_count"] + visit_count
            new_time = max(rec["last_visit_time"], last_visit_time)
            c.execute(
                "UPDATE urls SET visit_count = ?, last_visit_time = ?,"
                " title = CASE WHEN title = '' THEN ? ELSE title END WHERE id = ?",
                (new_count, new_time, title or "", rec["id"]),
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
    for url_str, visit_time, transition, visit_duration in edge_visits:
        chrome_url_id = url_id_map.get(url_str)
        if chrome_url_id is None:
            continue
        if (chrome_url_id, visit_time) in existing_visits:
            continue
        c.execute(
            "INSERT INTO visits (url, visit_time, from_visit, transition, segment_id, visit_duration)"
            " VALUES (?, ?, 0, ?, 0, ?)",
            (chrome_url_id, visit_time, transition, visit_duration),
        )
        existing_visits.add((chrome_url_id, visit_time))
        visits_added += 1

    conn.commit()
    conn.close()
    return urls_added, visits_added


def pick(label: str, items: list) -> list | None:
    if not items:
        print(f"No {label} found.")
        return None
    print(f"\n{label}:")
    for i, item in enumerate(items, 1):
        print(f"  [{i}] {item['name']}")
    sel = input(f"\nSelect (number, or 'all'): ").strip().lower()
    if sel == "all":
        return items
    try:
        idx = int(sel) - 1
        if 0 <= idx < len(items):
            return [items[idx]]
    except ValueError:
        pass
    print("Invalid selection.")
    return None


def main() -> int:
    print("--- Edge → Chrome History Import ---\n")

    edge_profiles = get_edge_profiles()
    if not edge_profiles:
        print("No Edge profiles found.")
        print("Expected:\n  " + "\n  ".join(EDGE_ROOTS))
        return 1

    chrome_profiles = get_chrome_profiles()
    if not chrome_profiles:
        print("No Chrome-family profiles found.")
        return 1

    sources = pick("Source Edge profiles", edge_profiles)
    if not sources:
        return 1

    targets = pick("Target browser profiles", chrome_profiles)
    if not targets:
        return 1

    target_names = ", ".join(t["name"] for t in targets)
    print(f"\n⚠  Make sure {target_names} is fully closed before continuing.")
    input("Press Enter when ready...")

    print("\nReading Edge history...")
    all_urls: list = []
    all_visits: list = []
    for src in sources:
        urls, visits = read_edge_history(src["history_path"])
        all_urls.extend(urls)
        all_visits.extend(visits)
        print(f"  + {len(urls):,} URLs, {len(visits):,} visits from {src['name']}")

    if not all_urls:
        print("No history to import.")
        return 1

    for target in targets:
        history_path = target["history_path"]
        backup_path = history_path + ".edge_import.bak"
        print(f"\nBacking up {target['name']} History → {os.path.basename(backup_path)}")
        shutil.copy2(history_path, backup_path)

        print(f"Merging into {target['name']}...")
        urls_added, visits_added = merge_into_chrome(all_urls, all_visits, history_path)
        print(f"  ✅ {urls_added:,} new URLs, {visits_added:,} new visits added")

    print("\nDone. Launch the browser to verify history.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
