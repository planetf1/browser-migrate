import sqlite3
import json
import os
import shutil
import sys

# --- CONFIGURATION ---
EDGE_ROOT = os.path.expanduser("~/Library/Application Support/Microsoft Edge")
EPOCH_DIFF_USEC = 11644473600 * 1000000

def get_profiles():
    """Scans Edge directory for valid profiles and their display names."""
    profiles = []

    # Standard Edge profiles are in 'Default' or 'Profile X' folders
    if not os.path.exists(EDGE_ROOT):
        return []

    # Iterate over all directories in Edge Root
    for item in os.listdir(EDGE_ROOT):
        full_path = os.path.join(EDGE_ROOT, item)

        # Check if it's a directory and has a History DB
        history_path = os.path.join(full_path, "History")
        preferences_path = os.path.join(full_path, "Preferences")

        if os.path.isdir(full_path) and os.path.exists(history_path):
            display_name = item # Default to folder name (e.g., "Profile 1")

            # Try to read the actual Profile Name from Preferences JSON
            if os.path.exists(preferences_path):
                try:
                    with open(preferences_path, 'r', encoding='utf-8') as f:
                        prefs = json.load(f)
                        # Edge stores the custom name here
                        display_name = prefs.get("profile", {}).get("name", item)

                        # Sometimes useful to show the email if available
                        email = prefs.get("account_info", [{}])[0].get("email", "")
                        if email:
                            display_name = f"{display_name} ({email})"
                except:
                    pass # Keep folder name if JSON fails

            profiles.append({
                "name": display_name,
                "folder": item,
                "db_path": history_path
            })

    return profiles

def export_profile(profile):
    """Exports history for a specific profile dictionary."""
    print(f"\n--- Processing: {profile['name']} ---")

    # Unique output filename based on profile name
    safe_name = "".join([c for c in profile['name'] if c.isalnum() or c in (' ', '-', '_')]).strip()
    output_filename = f"history_{safe_name.replace(' ', '_')}.json"
    temp_db = f"temp_{profile['folder']}.sqlite"

    try:
        # Copy DB to bypass lock
        shutil.copy2(profile['db_path'], temp_db)

        conn = sqlite3.connect(temp_db)
        cursor = conn.cursor()

        query = """
        SELECT urls.url, urls.title, visits.visit_time
        FROM visits
        JOIN urls ON visits.url = urls.id
        ORDER BY visits.visit_time DESC
        """
        cursor.execute(query)
        rows = cursor.fetchall()

        history_items = []
        for url, title, edge_time in rows:
            if not edge_time: continue

            unix_time_usec = edge_time - EPOCH_DIFF_USEC
            if unix_time_usec < 0: continue

            history_items.append({
                "url": url,
                "title": title or url,
                "time_usec": unix_time_usec
            })

        final_json = { "history": history_items }

        with open(output_filename, 'w', encoding='utf-8') as f:
            json.dump(final_json, f, indent=2)

        print(f"✅ Created: {output_filename} ({len(history_items)} items)")
        return output_filename

    except Exception as e:
        print(f"❌ Error exporting {profile['name']}: {e}")
        return None
    finally:
        if 'conn' in locals(): conn.close()
        if os.path.exists(temp_db): os.remove(temp_db)

def main():
    print(f"Scanning for Edge profiles in: {EDGE_ROOT}...\n")
    profiles = get_profiles()

    if not profiles:
        print("No Edge profiles found.")
        sys.exit(1)

    # List Profiles
    print("Found Profiles:")
    for idx, p in enumerate(profiles):
        print(f"[{idx + 1}] {p['name']}  (Path: .../{p['folder']})")

    # Selection Input
    selection = input("\nSelect profile number to export (or 'all'): ").strip().lower()

    generated_files = []

    if selection == 'all':
        for p in profiles:
            fname = export_profile(p)
            if fname: generated_files.append(fname)
    else:
        try:
            idx = int(selection) - 1
            if 0 <= idx < len(profiles):
                fname = export_profile(profiles[idx])
                if fname: generated_files.append(fname)
            else:
                print("Invalid selection.")
        except ValueError:
            print("Invalid input.")

    if generated_files:
        print("\n--- Summary ---")
        print("Import these files into Safari (File > Import From > Browsing Data from File...):")
        for f in generated_files:
            print(f" - {os.path.abspath(f)}")

if __name__ == "__main__":
    main()
