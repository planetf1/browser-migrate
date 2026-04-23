import sqlite3
import json
import os
import shutil
import sys

# --- CONFIGURATION ---
# We now scan all possible Edge channels on macOS
EDGE_ROOTS = [
    os.path.expanduser("~/Library/Application Support/Microsoft Edge"),        # Stable
    os.path.expanduser("~/Library/Application Support/Microsoft Edge Dev"),    # Dev
    os.path.expanduser("~/Library/Application Support/Microsoft Edge Beta"),   # Beta
    os.path.expanduser("~/Library/Application Support/Microsoft Edge Canary")  # Canary
]

EPOCH_DIFF_USEC = 11644473600 * 1000000

def get_profiles():
    all_profiles = []

    for root in EDGE_ROOTS:
        if not os.path.exists(root):
            continue

        channel_name = os.path.basename(root).replace("Microsoft Edge", "").strip() or "Stable"

        for item in os.listdir(root):
            full_path = os.path.join(root, item)
            history_path = os.path.join(full_path, "History")
            bookmarks_path = os.path.join(full_path, "Bookmarks")
            preferences_path = os.path.join(full_path, "Preferences")

            if os.path.isdir(full_path) and (os.path.exists(history_path) or os.path.exists(bookmarks_path)):
                display_name = item
                if os.path.exists(preferences_path):
                    try:
                        with open(preferences_path, 'r', encoding='utf-8') as f:
                            prefs = json.load(f)
                            name = prefs.get("profile", {}).get("name", item)
                            email = prefs.get("account_info", [{}])[0].get("email", "")
                            if email:
                                display_name = f"{name} ({email})"
                            else:
                                display_name = name
                    except:
                        pass

                all_profiles.append({
                    "channel": channel_name,
                    "name": display_name,
                    "folder": item,
                    "history_path": history_path,
                    "bookmarks_path": bookmarks_path
                })

    return all_profiles

def extract_history(profile):
    if not os.path.exists(profile['history_path']):
        print(f"   [History] ⚠️  No History DB found.")
        return None

    # Filename includes Channel (Dev/Stable) to avoid confusion
    safe_name = "".join([c for c in profile['name'] if c.isalnum() or c in (' ', '-', '_')]).strip()
    safe_channel = profile['channel'].replace(" ", "_")
    output_filename = f"History_{safe_channel}_{safe_name.replace(' ', '_')}.json"
    temp_db = f"temp_history_{profile['folder']}.sqlite"

    shutil.copy2(profile['history_path'], temp_db)

    try:
        conn = sqlite3.connect(temp_db)
        cursor = conn.cursor()

        cursor.execute("SELECT count(*) FROM visits")
        total_raw = cursor.fetchone()[0]

        query = """
        SELECT urls.url, urls.title, visits.visit_time
        FROM visits
        LEFT JOIN urls ON visits.url = urls.id
        WHERE visits.visit_time > 0
        ORDER BY visits.visit_time DESC
        """
        cursor.execute(query)
        rows = cursor.fetchall()

        history_items = []
        for url, title, edge_time in rows:
            if not url: continue

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

        print(f"   [History] ✅ Extracted {len(history_items)} items (Raw DB: {total_raw}) -> {output_filename}")
        return output_filename

    except Exception as e:
        print(f"   [History] ❌ Error: {e}")
        return None
    finally:
        if 'conn' in locals(): conn.close()
        if os.path.exists(temp_db): os.remove(temp_db)

def extract_bookmarks(profile):
    if not os.path.exists(profile['bookmarks_path']):
        print(f"   [Bookmarks] ⚠️  No Bookmarks file found.")
        return None

    safe_name = "".join([c for c in profile['name'] if c.isalnum() or c in (' ', '-', '_')]).strip()
    safe_channel = profile['channel'].replace(" ", "_")
    output_filename = f"Bookmarks_{safe_channel}_{safe_name.replace(' ', '_')}.html"

    try:
        with open(profile['bookmarks_path'], 'r', encoding='utf-8') as f:
            data = json.load(f)

        roots = data.get('roots', {})
        html_content = [
            "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
            "",
            "<TITLE>Bookmarks</TITLE>",
            "<H1>Bookmarks</H1>",
            "<DL><p>"
        ]

        def parse_node(node):
            if node['type'] == 'url':
                url = node.get('url', '')
                name = node.get('name', 'Untitled')
                html_content.append(f'<DT><A HREF="{url}">{name}</A>')
            elif node['type'] == 'folder':
                name = node.get('name', 'Folder')
                html_content.append(f'<DT><H3>{name}</H3>')
                html_content.append('<DL><p>')
                for child in node.get('children', []):
                    parse_node(child)
                html_content.append('</DL><p>')

        for root_key in ['bookmark_bar', 'other', 'synced']:
            if root_key in roots:
                parse_node(roots[root_key])

        html_content.append("</DL><p>")

        with open(output_filename, 'w', encoding='utf-8') as f:
            f.write("\n".join(html_content))

        print(f"   [Bookmarks] ✅ Converted -> {output_filename}")
        return output_filename

    except Exception as e:
        print(f"   [Bookmarks] ❌ Error: {e}")
        return None

def main():
    print("Scanning ALL Edge Channels (Stable, Dev, Beta, Canary)...\n")
    profiles = get_profiles()

    if not profiles:
        print("No Edge profiles found in any channel.")
        sys.exit(1)

    print("Found Profiles:")
    for idx, p in enumerate(profiles):
        print(f"[{idx + 1}] [{p['channel']}] {p['name']}")

    selection = input("\nSelect profile number to export (or 'all'): ").strip().lower()

    if selection == 'all':
        targets = profiles
    else:
        try:
            targets = [profiles[int(selection) - 1]]
        except:
            print("Invalid selection")
            sys.exit(1)

    for p in targets:
        print(f"\n--- Processing: {p['name']} ({p['channel']}) ---")
        extract_history(p)
        extract_bookmarks(p)

if __name__ == "__main__":
    main()
