import sqlite3
import json
import os
import shutil
import sys
import time
from datetime import datetime

# --- CONFIGURATION ---
# Auto-detects all Edge channels
EDGE_ROOTS = [
    os.path.expanduser("~/Library/Application Support/Microsoft Edge"),
    os.path.expanduser("~/Library/Application Support/Microsoft Edge Dev"),
    os.path.expanduser("~/Library/Application Support/Microsoft Edge Beta"),
    os.path.expanduser("~/Library/Application Support/Microsoft Edge Canary")
]

# Edge (1601) to Unix (1970) in microseconds
EPOCH_DIFF_USEC = 11644473600 * 1000000

def get_profiles():
    all_profiles = []
    for root in EDGE_ROOTS:
        if not os.path.exists(root): continue
        channel = os.path.basename(root).replace("Microsoft Edge", "").strip() or "Stable"

        for item in os.listdir(root):
            full_path = os.path.join(root, item)
            history_path = os.path.join(full_path, "History")
            prefs_path = os.path.join(full_path, "Preferences")

            if os.path.isdir(full_path) and os.path.exists(history_path):
                display_name = f"{channel} - {item}"
                if os.path.exists(prefs_path):
                    try:
                        with open(prefs_path, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            name = data.get("profile", {}).get("name", item)
                            email = data.get("account_info", [{}])[0].get("email", "")
                            if email: display_name = f"{display_name} ({email})"
                    except: pass

                all_profiles.append({
                    "id": f"{channel}_{item}",
                    "name": display_name,
                    "history_path": history_path
                })
    return all_profiles

def extract_history_strict(profile):
    """Extracts history matching Apple's strict Native JSON schema."""
    if not os.path.exists(profile['history_path']): return []

    temp_db = f"temp_{profile['id']}.sqlite"
    shutil.copy2(profile['history_path'], temp_db)
    items = []

    try:
        conn = sqlite3.connect(temp_db)
        cursor = conn.cursor()

        # Edge stores 'visit_duration' which we can use to detect failed loads (optional)
        cursor.execute("""
            SELECT urls.url, urls.title, visits.visit_time
            FROM visits
            LEFT JOIN urls ON visits.url = urls.id
            WHERE visits.visit_time > 0
        """)

        for url, title, edge_time in cursor.fetchall():
            if not url: continue

            unix_time = int(edge_time - EPOCH_DIFF_USEC)
            if unix_time < 0: continue

            # --- APPLE NATIVE SCHEMA ---
            # Ref: Apple Developer "Importing data exported from Safari"
            items.append({
                "url": str(url),
                "title": str(title) if title else str(url),
                "time_usec": unix_time,      # Must be Integer
                "visits_count": 1,           # Must be Integer, >= 1
                # Optional fields Safari appreciates:
                "latest_visit_was_load_failure": False,
                "latest_visit_was_http_get": True
            })

    except Exception as e:
        print(f"⚠️ Error reading {profile['name']}: {e}")
    finally:
        if 'conn' in locals(): conn.close()
        if os.path.exists(temp_db): os.remove(temp_db)

    return items

def main():
    print("--- Edge to Safari (Native Apple Format) ---\n")
    profiles = get_profiles()
    if not profiles:
        print("No profiles found.")
        sys.exit(1)

    # 1. Profile Selection
    for idx, p in enumerate(profiles):
        print(f"[{idx + 1}] {p['name']}")

    selection = input("\nSelect profile (or 'all'): ").strip().lower()

    selected_profiles = profiles if selection == 'all' else [profiles[int(selection)-1]]

    # 2. Extract & Merge
    all_items = []
    print("\nExtracting...")
    for p in selected_profiles:
        items = extract_history_strict(p)
        all_items.extend(items)
        print(f"   + {len(items)} items from {p['name']}")

    # 3. Deduplicate
    print(f"Deduplicating {len(all_items)} items...")
    unique_map = {f"{x['time_usec']}_{x['url']}": x for x in all_items}
    final_list = list(unique_map.values())
    final_list.sort(key=lambda x: x['time_usec']) # Sorted is safer

    # 4. Construct Final JSON with Metadata
    # Safari REQUIRES the metadata key to validate the file
    final_json = {
        "metadata": {
            "version": "1.0",
            "export_date_time_usec": int(time.time() * 1000000),
            "browser": "Safari_Migrator"
        },
        "history": final_list
    }

    # 5. Save STRICTLY as 'History.json'
    # Safari looks for this specific filename when importing folders,
    # but we will select the file directly.
    output_filename = "History.json"

    with open(output_filename, 'w', encoding='utf-8') as f:
        json.dump(final_json, f, indent=2)

    print(f"\n✅ SUCCESS! Generated: {output_filename} ({len(final_list)} items)")
    print("\n--- FINAL INSTRUCTIONS ---")
    print("1. Open Safari")
    print("2. File > Import From > Browsing Data from File...")
    print(f"3. Select the file '{output_filename}' (Do not rename it)")

if __name__ == "__main__":
    main()
