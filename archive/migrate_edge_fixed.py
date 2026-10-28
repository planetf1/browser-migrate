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
    all_profiles = []
    for root in EDGE_ROOTS:
        if not os.path.exists(root): continue
        channel = os.path.basename(root).replace("Microsoft Edge", "").strip() or "Stable"

        for item in os.listdir(root):
            full_path = os.path.join(root, item)
            history_path = os.path.join(full_path, "History")

            if os.path.isdir(full_path) and os.path.exists(history_path):
                # Simple name generation
                all_profiles.append({
                    "id": f"{channel}_{item}",
                    "name": f"{channel} - {item}",
                    "history_path": history_path
                })
    return all_profiles

def extract_history_items(profile):
    if not os.path.exists(profile['history_path']): return []

    temp_db = f"temp_{profile['id']}.sqlite"
    shutil.copy2(profile['history_path'], temp_db)
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

            # Convert timestamp
            unix_time = edge_time - EPOCH_DIFF_USEC
            if unix_time < 0: continue

            # --- GOOGLE TAKEOUT SCHEMA ---
            # Safari expects this specific structure
            items.append({
                "page_transition": "LINK",
                "title": title or "Untitled",
                "url": url,
                "client_id": str(uuid.uuid4()), # Fake ID to satisfy schema
                "time_usec": unix_time,
                # Internal key for deduplication
                "_dedup_key": f"{unix_time}_{url}"
            })

    except Exception as e:
        print(f"⚠️ Error reading {profile['name']}: {e}")
    finally:
        if 'conn' in locals(): conn.close()
        if os.path.exists(temp_db): os.remove(temp_db)

    print(f"   + Loaded {len(items)} items from {profile['name']}")
    return items

def main():
    print("--- Edge to Safari (Google Takeout Format) ---\n")
    profiles = get_profiles()

    if not profiles:
        print("No profiles found.")
        sys.exit(1)

    all_history = []

    print("Step 1: extracting...")
    for p in profiles:
        all_history.extend(extract_history_items(p))

    print(f"\nStep 2: Deduplicating {len(all_history)} items...")

    # Deduplicate
    unique_map = {item['_dedup_key']: item for item in all_history}
    final_list = []

    for item in unique_map.values():
        del item['_dedup_key'] # Remove internal key
        final_list.append(item)

    # Sort by time (Newest last)
    final_list.sort(key=lambda x: x['time_usec'])

    # --- WRITING THE FILE ---
    # The key MUST be "Browser History" (with space) for Safari to see it
    final_json = {
        "Browser History": final_list
    }

    outfile = "Safari_Import_Fixed.json"
    with open(outfile, 'w', encoding='utf-8') as f:
        json.dump(final_json, f, indent=2)

    print(f"\n✅ SUCCESS! Created '{outfile}' with {len(final_list)} items.")
    print("This file matches the Google Takeout schema.")
    print("\nAttempt Import in Safari again:")
    print(f"File > Import From > Browsing Data from File... -> Select {outfile}")

if __name__ == "__main__":
    main()
