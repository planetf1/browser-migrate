
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Edge → Safari History JSON (macOS)

Produces Safari's importable History.json format:
{
  "metadata": {
    "browser_name": "...",
    "browser_version": "...",
    "data_type": "history",
    "export_time_usec": <unix microseconds>,
    "schema_version": 1
  },
  "history": [
      { "url": "...", "title": "...", "time_usec": 123, "visits_count": 1 }
  ]
}
"""

import sqlite3
import json
import os
import shutil
import sys
import time
from collections import defaultdict

# --- CONFIGURATION (macOS paths to Edge channels) ---
EDGE_ROOTS = [
    os.path.expanduser("~/Library/Application Support/Microsoft Edge"),
    os.path.expanduser("~/Library/Application Support/Microsoft Edge Dev"),
    os.path.expanduser("~/Library/Application Support/Microsoft Edge Beta"),
    os.path.expanduser("~/Library/Application Support/Microsoft Edge Canary"),
]

# Chromium/Edge epoch (1601) → Unix (1970) conversion in microseconds
EPOCH_DIFF_USEC = 11644473600 * 1_000_000


def get_channel_name_from_root(root_path: str) -> str:
    base = os.path.basename(root_path)
    if base == "Microsoft Edge":
        return "Microsoft Edge"
    if base == "Microsoft Edge Dev":
        return "Microsoft Edge Dev"
    if base == "Microsoft Edge Beta":
        return "Microsoft Edge Beta"
    if base == "Microsoft Edge Canary":
        return "Microsoft Edge Canary"
    return "Microsoft Edge"


def get_channel_version(root_path: str) -> str:
    """
    Attempt to read Edge's version from 'Local State' JSON in the channel root.
    Returns 'unknown' if not available.
    """
    local_state = os.path.join(root_path, "Local State")
    if not os.path.exists(local_state):
        return "unknown"
    try:
        with open(local_state, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Try common locations for a version field
        if isinstance(data, dict):
            if "browser" in data and isinstance(data["browser"], dict):
                v = data["browser"].get("last_version") or data["browser"].get("version")
                if v:
                    return str(v)
            v = data.get("last_version") or data.get("version")
            if v:
                return str(v)
    except Exception:
        pass
    return "unknown"


def get_profiles():
    """
    Scan all Edge channels and return a list of valid profiles.
    A profile is any directory under the root that contains a 'History' DB.
    """
    profiles = []
    for root in EDGE_ROOTS:
        if not os.path.exists(root):
            continue

        channel_name = get_channel_name_from_root(root)
        channel_version = get_channel_version(root)

        for item in os.listdir(root):
            full_path = os.path.join(root, item)
            history_path = os.path.join(full_path, "History")
            prefs_path = os.path.join(full_path, "Preferences")

            if os.path.isdir(full_path) and os.path.exists(history_path):
                display_name = f"{channel_name} - {item}"
                # Try to get friendly profile name (e.g., "Personal", "Work")
                if os.path.exists(prefs_path):
                    try:
                        with open(prefs_path, "r", encoding="utf-8") as f:
                            prefs = json.load(f)
                        name = prefs.get("profile", {}).get("name", item)
                        email = ""
                        acc_info = prefs.get("account_info")
                        if isinstance(acc_info, list) and acc_info:
                            email = acc_info[0].get("email", "")
                        if email:
                            display_name = f"{channel_name} - {name} ({email})"
                        else:
                            display_name = f"{channel_name} - {name}"
                    except Exception:
                        pass

                profiles.append({
                    "id": f"{channel_name}_{item}",
                    "name": display_name,
                    "history_path": history_path,
                    "safe_filename": f"{channel_name}_{item}".replace(" ", "_"),
                    "channel_root": root,
                    "channel_name": channel_name,
                    "channel_version": channel_version,
                })
    return profiles


def extract_history_items(profile):
    """
    Extract one row per URL using Edge/Chromium 'urls' table:
      - urls.url
      - urls.title
      - urls.visit_count
      - urls.last_visit_time (microseconds since 1601)
    Convert to Safari items:
      { url, title, time_usec (latest visit), visits_count }
    """
    path = profile["history_path"]
    if not os.path.exists(path):
        return []

    temp_db = f"temp_{profile['id']}.sqlite"
    items = []

    try:
        shutil.copy2(path, temp_db)
    except IOError:
        print(f"⚠️ Could not copy History DB for {profile['name']} (maybe locked).")
        return []

    try:
        conn = sqlite3.connect(temp_db)
        cursor = conn.cursor()

        # Using 'urls' gives a single aggregate per URL with last_visit_time/visit_count
        cursor.execute("""
            SELECT urls.url, urls.title, urls.visit_count, urls.last_visit_time
            FROM urls
            WHERE urls.url IS NOT NULL
        """)

        rows = cursor.fetchall()
        for url, title, visit_count, edge_last in rows:
            if not url:
                continue
            # Edge → Unix microseconds
            unix_time = int(edge_last - EPOCH_DIFF_USEC)
            if unix_time <= 0:
                # skip negative/invalid times
                continue

            items.append({
                "url": url,
                "title": (title or "Untitled"),
                "time_usec": unix_time,
                "visits_count": int(visit_count or 1),
            })

    except Exception as e:
        print(f"⚠️ Error reading {profile['name']}: {e}")
    finally:
        try:
            if 'conn' in locals():
                conn.close()
        except Exception:
            pass
        try:
            if os.path.exists(temp_db):
                os.remove(temp_db)
        except Exception:
            pass

    print(f"   + Loaded {len(items)} URLs from {profile['name']}")
    return items


def aggregate_across_profiles(items):
    """
    Aggregate when merging multiple profiles:
    - Group by URL
    - visits_count := sum across profiles
    - time_usec   := max (latest visit across profiles)
    - title       := prefer a non-empty title from the latest visit, otherwise keep any
    """
    by_url = defaultdict(lambda: {"visits_count": 0, "time_usec": 0, "title": ""})

    for it in items:
        url = it["url"]
        vc = max(1, int(it.get("visits_count", 1)))
        tu = int(it.get("time_usec", 0))
        title = (it.get("title") or "").strip()

        entry = by_url[url]
        entry["visits_count"] += vc
        if tu >= entry["time_usec"]:
            entry["time_usec"] = tu
            # Prefer title from the latest visit if available
            if title:
                entry["title"] = title
        else:
            # If we don't have any title yet, take any non-empty
            if not entry["title"] and title:
                entry["title"] = title

    aggregated = []
    for url, data in by_url.items():
        aggregated.append({
            "url": url,
            "title": data["title"] if data["title"] else "Untitled",
            "time_usec": data["time_usec"],
            "visits_count": max(1, int(data["visits_count"])),
        })
    return aggregated


def save_safari_history(items, filename, source_name="Microsoft Edge", source_version="unknown"):
    """
    Emit Safari's importable JSON schema:
    {
      "metadata": {
        "browser_name": "...",
        "browser_version": "...",
        "data_type": "history",
        "export_time_usec": <unix microseconds>,
        "schema_version": 1
      },
      "history": [ { url, title?, time_usec, visits_count, ... } ]
    }
    """
    # Sort chronologically (optional but tidy)
    items_sorted = sorted(items, key=lambda x: x["time_usec"])

    safari_json = {
        "metadata": {
            "browser_name": source_name,
            "browser_version": source_version,
            "data_type": "history",
            "export_time_usec": int(time.time() * 1_000_000),
            "schema_version": 1,
        },
        "history": items_sorted,
    }

    with open(filename, "w", encoding="utf-8") as f:
        json.dump(safari_json, f, indent=2)

    print(f"✅ Created: {filename} ({len(items_sorted)} items)")


def main() -> int:
    print("--- Edge → Safari History (Apple schema) ---\n")
    profiles = get_profiles()

    if not profiles:
        print("No Edge profiles found.\n")
        print("Expected roots:\n  - " + "\n  - ".join(EDGE_ROOTS))
        return 1

    # 1) Menu
    print("Found Profiles:")
    for idx, p in enumerate(profiles):
        print(f"[{idx + 1}] {p['name']}")

    # 2) Input
    selection = input("\nSelect profile number (or type 'all'): ").strip().lower()

    selected_profiles = []
    is_unified = False

    if selection == "all":
        selected_profiles = profiles
        is_unified = True
    else:
        try:
            idx = int(selection) - 1
            if 0 <= idx < len(profiles):
                selected_profiles = [profiles[idx]]
            else:
                print("Invalid selection.")
                return 1
        except ValueError:
            print("Invalid input.")
            return 1

    # 3) Process
    if is_unified:
        # Merge ALL profiles into a single History.json
        print(f"\nMerging {len(selected_profiles)} profiles...")
        all_items = []
        # Use channel info from the first profile for metadata
        source_name = "Microsoft Edge (All Profiles)"
        source_version = selected_profiles[0]["channel_version"] if selected_profiles else "unknown"

        for p in selected_profiles:
            all_items.extend(extract_history_items(p))

        aggregated = aggregate_across_profiles(all_items)
        save_safari_history(aggregated, "History.json", source_name=source_name, source_version=source_version)
    else:
        # Single profile → one file
        p = selected_profiles[0]
        print(f"\nProcessing: {p['name']}...")
        items = extract_history_items(p)
        out_name = f"History_{p['safe_filename']}.json"
        save_safari_history(items, out_name, source_name=p["channel_name"], source_version=p["channel_version"])

    print("\n--- Next Step ---")
    print("Open Safari → File → Import From → Browsing Data from File…")
    print("Choose the History.json file you just created.")
    print("\n(You can also zip multiple files and import the ZIP.)")

    return 0


if __name__ == "__main__":
    main()
