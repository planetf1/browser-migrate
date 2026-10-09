#!/usr/bin/env python3
"""
safari_to_edge.py — pull Safari browsing history back into Edge Beta,
per profile, without duplicates.

The reverse of direct_import_safari.py: reads each Safari profile's
History.db (default → personal, work UUID → work) and merges its visits
into the matching Edge Beta profile's Chromium History DB. HISTORY ONLY —
this tool never reads or writes bookmarks (Bookmarks.plist is never
opened).

Dedupe is (url, visit_time ± 2 µs): the forward import stored Edge's
integer µs as Cocoa REAL seconds, and the double round-trip drifts by
1–2 µs, so exact-integer matching would re-insert ~half the round-trip
rows as near-duplicates. A ±2 µs tolerance catches them all — genuinely
distinct visits of the same URL are never microseconds apart — so only
genuinely new visits (native Safari browsing) insert.

Safari may stay running (databases are snapshot-copied to a temp dir
before reading). For --apply, Edge must be fully quit; each target
History file gets a timestamped backup first.

Usage:
  uv run safari_to_edge.py                              # dry run, all
  uv run safari_to_edge.py --since 2026-10-07           # only recent
  uv run safari_to_edge.py --since 2026-10-07 --apply
  uv run safari_to_edge.py --profile work               # one profile only
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime

# Cocoa (Safari, seconds since 2001-01-01) -> Chromium (µs since 1601-01-01)
COCOA_TO_CHROMIUM = 978_307_200 + 11_644_473_600
# Float round-trip drift between Edge's integer µs and Safari's REAL seconds
TOLERANCE_US = 2
WORK_PROFILE_PREFIX = "1B5279A1"
EDGE_ROOT = os.path.expanduser("~/Library/Application Support/Microsoft Edge Beta")

# Safari profile -> Edge Beta profile directory + expected account email
# (the email is re-verified from Preferences at every run; a mismatch aborts)
PROFILE_MAP = {
    "personal": ("default", "Default", "nigel@cherrybyte.me.uk"),
    "work": (WORK_PROFILE_PREFIX, "Profile 1", "jonesn@uk.ibm.com"),
}


def die(msg: str) -> None:
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def safari_db(profile_key: str) -> str:
    if profile_key == "default":
        p = os.path.expanduser("~/Library/Safari/History.db")
    else:
        base = os.path.expanduser(
            "~/Library/Containers/com.apple.Safari/Data/Library/Safari/Profiles"
        )
        matches = [d for d in os.listdir(base) if d.startswith(profile_key)]
        if len(matches) != 1:
            die(f"Safari profile prefix {profile_key!r} matched {matches}")
        p = os.path.join(base, matches[0], "History.db")
    if not os.path.exists(p):
        die(f"missing {p}")
    return p


def snapshot(db_path: str, tmpdir: str, name: str) -> str:
    """Copy db+wal+shm so the source browser can stay running."""
    dst = os.path.join(tmpdir, name)
    shutil.copy2(db_path, dst)
    for ext in ("-wal", "-shm"):
        if os.path.exists(db_path + ext):
            shutil.copy2(db_path + ext, dst + ext)
    return dst


def read_safari_visits(db_path: str) -> list[tuple[str, str, int]]:
    """[(url, title, chromium_visit_time_us), ...] — title from latest visit."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT h.url, v.title, v.visit_time, h.id "
        "FROM history_visits v JOIN history_items h ON h.id = v.history_item "
        "ORDER BY v.visit_time"
    ).fetchall()
    conn.close()
    latest_title: dict[int, str] = {}
    out: list[tuple[str, str, int]] = []
    for url, title, vt, hid in rows:
        if vt is None or vt <= 0:
            continue
        if title:
            latest_title[hid] = title
        out.append((url, latest_title.get(hid, ""), round((vt + COCOA_TO_CHROMIUM) * 1e6)))
    return out


def edge_identity(edge_dir: str) -> str:
    prefs = os.path.join(EDGE_ROOT, edge_dir, "Preferences")
    try:
        with open(prefs, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("account_info", [{}])[0].get("email", "(signed out)")
    except (OSError, json.JSONDecodeError):
        return "(unknown)"


def merge_into_edge(edge_db: str, visits: list[tuple[str, str, int]],
                    apply: bool) -> tuple[int, int, list[tuple[str, int]]]:
    """Tolerance-dedupe against (url, visit_time); insert-only.
    Returns (new_urls, new_visits, [(url, time_us)] inserted)."""
    conn = sqlite3.connect(edge_db)
    c = conn.cursor()
    existing_urls: dict[str, int] = {
        row[1]: row[0] for row in c.execute("SELECT id, url FROM urls")
    }
    edge_times: dict[str, list[int]] = defaultdict(list)
    for url, vt in c.execute(
        "SELECT u.url, v.visit_time FROM visits v JOIN urls u ON u.id = v.url"
    ):
        edge_times[url].append(vt)
    for lst in edge_times.values():
        lst.sort()

    def seen(url: str, t: int) -> bool:
        lst = edge_times.get(url)
        if not lst:
            return False
        i = bisect_left(lst, t - TOLERANCE_US)
        return i < len(lst) and lst[i] <= t + TOLERANCE_US

    per_url: dict[str, list[int]] = defaultdict(list)
    url_title: dict[str, str] = {}
    for url, title, t in visits:
        per_url[url].append(t)
        if title:
            url_title[url] = title

    urls_added = 0
    inserted: list[tuple[str, int]] = []
    inserted_keys: set[tuple[int, int]] = set()
    for url, times in per_url.items():
        uid = existing_urls.get(url)
        new_times = [t for t in times if not seen(url, t)]
        if uid is None:
            urls_added += 1
            if apply:
                c.execute(
                    "INSERT INTO urls (url, title, visit_count, typed_count,"
                    " last_visit_time, hidden) VALUES (?, ?, ?, 0, ?, 0)",
                    (url, url_title.get(url, ""), len(new_times), max(times)),
                )
                uid = c.lastrowid
                existing_urls[url] = uid
            else:
                uid = -1  # counting placeholder
        elif apply and new_times:
            c.execute(
                "UPDATE urls SET visit_count = visit_count + ?,"
                " last_visit_time = MAX(last_visit_time, ?) WHERE id = ?",
                (len(new_times), max(times), uid),
            )
        for t in new_times:
            if (uid, t) in inserted_keys:
                continue
            if apply and uid > 0:
                c.execute(
                    "INSERT INTO visits (url, visit_time, from_visit,"
                    " transition, segment_id, visit_duration)"
                    " VALUES (?, ?, 0, 1, 0, 0)",
                    (uid, t),
                )
                inserted_keys.add((uid, t))
            inserted.append((url, t))

    if apply:
        conn.commit()
    conn.close()
    return urls_added, len(inserted), inserted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", choices=["personal", "work"], default=None)
    ap.add_argument("--since", type=str, default=None,
                    help="only visits on/after this local date (YYYY-MM-DD)")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    since_cocoa = None
    if a.since:
        try:
            dt = datetime.strptime(a.since, "%Y-%m-%d")
        except ValueError:
            die(f"bad --since {a.since!r} (want YYYY-MM-DD)")
        since_cocoa = time.mktime(dt.timetuple()) - 978_307_200

    if a.apply:
        r = subprocess.run(["pgrep", "-if", "Microsoft Edge Beta.app"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            die("Edge is running — quit it (⌘Q) first, then retry.")

    tmpdir = tempfile.mkdtemp(prefix="s2e-")
    ts = time.strftime("%Y%m%d-%H%M%S")
    selected = [a.profile] if a.profile else ["personal", "work"]

    grand_urls = grand_visits = 0
    for prof in selected:
        safari_key, edge_dir, expected_email = PROFILE_MAP[prof]
        edge_db = os.path.join(EDGE_ROOT, edge_dir, "History")
        if not os.path.exists(edge_db):
            die(f"missing {edge_db}")
        email = edge_identity(edge_dir)
        if email != expected_email:
            die(f"Edge Beta {edge_dir!r} resolved to {email!r}, expected "
                f"{expected_email!r} — profile layout changed; refusing to "
                f"write to the wrong profile. Update PROFILE_MAP.")

        snap_safari = snapshot(safari_db(safari_key), tmpdir, f"safari-{prof}.db")
        snap_edge = snapshot(edge_db, tmpdir, f"edge-{prof}.db")

        visits = read_safari_visits(snap_safari)
        if since_cocoa is not None:
            since_us = round((since_cocoa + COCOA_TO_CHROMIUM) * 1e6)
            visits = [v for v in visits if v[2] >= since_us]

        edge_db_for_merge = snap_edge if not a.apply else edge_db
        if a.apply:
            for ext in ("", "-wal", "-shm"):
                src = edge_db + ext
                if os.path.exists(src):
                    shutil.copy2(src, f"{src}.safari-import-{ts}.bak")

        n_urls, n_visits, inserted = merge_into_edge(edge_db_for_merge, visits, a.apply)

        day_counts: dict[str, int] = defaultdict(int)
        for _, t in inserted:
            day = datetime.fromtimestamp(t / 1e6 - 11_644_473_600).strftime("%Y-%m-%d")
            day_counts[day] += 1
        print(f"── {prof}: Safari ({safari_key}) → Edge Beta {edge_dir!r} ({email}) ──")
        print(f"Safari visits read: {len(visits):,}")
        print(f"  already in Edge (url+time dedupe): {len(visits) - n_visits:,}")
        print(f"  would insert: {n_visits:,} visits across {n_urls:,} new URLs"
              f"{'  (DRY RUN)' if not a.apply else ''}")
        if a.apply:
            print(f"  inserted: {n_visits:,} visits, {n_urls:,} URLs "
                  f"(backup: History.safari-import-{ts}.bak)")
        if inserted:
            days = sorted(day_counts.items(), reverse=True)
            line = "  by day: " + "  ".join(f"{d}×{n}" for d, n in days[:12])
            if len(days) > 12:
                line += f"  …+{len(days) - 12} more days"
            print(line)
            for url, t in sorted(inserted, key=lambda p: -p[1])[:5]:
                print(f"  e.g. {datetime.fromtimestamp(t / 1e6 - 11_644_473_600).strftime('%Y-%m-%d %H:%M')} {url[:72]}")
        grand_urls += n_urls
        grand_visits += n_visits
        print()

    if not a.apply:
        print(f"TOTAL would insert: {grand_visits:,} visits, {grand_urls:,} new URLs "
              f"(dry run — add --apply to write, --since to filter by date)")
    else:
        print(f"TOTAL inserted: {grand_visits:,} visits, {grand_urls:,} new URLs. "
              f"Launch Edge to verify (⌘Y).")
    shutil.rmtree(tmpdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
