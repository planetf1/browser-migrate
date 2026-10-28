import sqlite3
import json
import os
import shutil
import sys
import uuid

# --- CONFIGURATION ---
EDGE_ROOTS = [
    os.path.expanduser("~/Library/Application Support/Microsoft Edge"),
    os.path.expanduser("~/Library/Application Support/Microsoft Edge Dev"),
    os.path.expanduser("~/Library/Application Support/Microsoft Edge Beta"),
    os.path.expanduser("~/Library/Application Support/Microsoft Edge Canary")
]

# Edge (1601) to Unix (1970) in microseconds
EPOCH_DIFF_USEC = 11644473600 * 1000000

def get_profiles():
    """Scans all Edge channels and returns a list of valid profiles."""
    all_profiles = []
    for root in EDGE_ROOTS:
        if not os.path.exists(root): continue
        channel = os.path.basename(root).replace("Microsoft Edge", "").strip() or "Stable"

        for item in os.listdir(root):
            full_path = os.path.join(root, item)
            history_path = os.path.join(full_path, "History")
            prefs_path = os.path.join(full_path, "Preferences")

            if os.path.isdir(full_path) and os.path.exists(history_path):
                # Try to get a real name (e.g. "Work", "Personal")
                display_name = f"{channel} - {item}"
                if os.path.exists(prefs_path):
                    try:
                        with open(prefs_path, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            name = data.get("profile", {}).get("name", item)
                            email = data.get("account_info", [{}])[0].get("email", "")
                            if email:
                                display_name = f"{channel} - {name} ({email})"
                            else:
                                display_name = f"{channel} - {name}"
                    except: pass

                all_profiles.append({
                    "id": f"{channel}_{item}",
                    "name": display_name,
                    "history_path": history_path,
                    "safe_filename": f"{channel}_{item}".replace(" ", "_")
                })
    return all_profiles

def extract_history_items(profile):
    """Extracts raw history items converted to Google Takeout Schema."""
    if not os.path.exists(profile['history_path']): return []

    temp_db = f"temp_{profile['id']}.sqlite"
    # Copy DB to avoid locks
    try:
        shutil.copy2(profile['history_path'], temp_db)
    except IOError:
        return []

    items = []
    try:
        conn = sqlite3.connect(temp_db)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT urls.url, urls.title, visits.visit_time
            FROM visits
            LEFT JOIN urls ON visits.url = urls.id
            WHERE visits.visit_time > 0
        """)

        for url, title, edge_time in cursor.fetchall():
            if not url: continue

            unix_time = edge_time - EPOCH_DIFF_USEC
            if unix_time < 0: continue

            # --- SAFARI / GOOGLE TAKEOUT SCHEMA ---
            items.append({
                "page_transition": "LINK",
                "title": title or "Untitled",
                "url": url,
                "client_id": str(uuid.uuid4()),
                "time_usec": unix_time,
                "_dedup_key": f"{unix_time}_{url}" # Internal use only
            })

    except Exception as e:
        print(f"⚠️ Error reading {profile['name']}: {e}")
    finally:
        if 'conn' in locals(): conn.close()
        if os.path.exists(temp_db): os.remove(temp_db)

    print(f"   + Loaded {len(items)} items from {profile['name']}")
    return items

def save_json(items, filename):
    """Deduplicates and saves to the strict JSON format Safari expects."""

    # Deduplicate (just in case the DB has internal dupes or we merged profiles)
    unique_map = {item['_dedup_key']: item for item in items}
    final_list = []
    for item in unique_map.values():
        del item['_dedup_key'] # Clean up internal key
        final_list.append(item)

    # Sort Chronologically
    final_list.sort(key=lambda x: x['time_usec'])

    # The Magic Key: "Browser History"
    final_json = { "Browser History": final_list }

    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(final_json, f, indent=2)

    print(f"✅ Created: {filename} ({len(final_list)} unique items)")

def main():
    print("--- Edge to Safari Select (Google Format) ---\n")
    profiles = get_profiles()

    if not profiles:
        print("No profiles found.")
        sys.exit(1)

    # 1. Display Menu
    print("Found Profiles:")
    for idx, p in enumerate(profiles):
        print(f"[{idx + 1}] {p['name']}")

    # 2. Get Input
    selection = input("\nSelect profile number (or 'all'): ").strip().lower()

    selected_profiles = []
    is_unified = False

    if selection == 'all':
        selected_profiles = profiles
        is_unified = True
    else:
        try:
            idx = int(selection) - 1
            if 0 <= idx < len(profiles):
                selected_profiles = [profiles[idx]]
            else:
                print("Invalid selection.")
                sys.exit(1)
        except ValueError:
            print("Invalid input.")
            sys.exit(1)

    # 3. Process
    if is_unified:
        # Merge ALL into one file
        print(f"\nMerging {len(selected_profiles)} profiles...")
        all_items = []
        for p in selected_profiles:
            all_items.extend(extract_history_items(p))
        save_json(all_items, "Safari_Import_Unified.json")
    else:
        # Process SINGLE profile
        p = selected_profiles[0]
        print(f"\nProcessing: {p['name']}...")
        items = extract_history_items(p)
        filename = f"Safari_Import_{p['safe_filename']}.json"
        save_json(items, filename)

    print("\n--- Next Step ---")
    print("Open Safari > File > Import From > Browsing Data from File...")

if __name__ == "__main__":
    main()
