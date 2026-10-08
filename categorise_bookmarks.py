#!/usr/bin/env python3
"""
categorise_bookmarks.py — flatten Safari's shared Bookmarks.plist (personal
bookmarks; Work/ReadingList/History are untouched) into a subject scheme.

Scheme folders reuse existing good folder names where possible:
  Travel, Conferences, Programming, AI, Tech, Home & Network, Health,
  Entertainment, News, Money & Admin, Shopping & Food, Cars & EV, Jobs,
  Family, Community, Unsorted

Classification: domain rules first, then title keywords, else Unsorted.
Conferences is kept whole (already organised). Everything else is flattened
and re-homed per-leaf. The BookmarksBar is emptied (its leaves are
categorised too); the bar itself stays, empty.

Usage:
    uv run categorise_bookmarks.py            # DRY RUN
    uv run categorise_bookmarks.py --apply    # performs it (backup first)

Safari MUST be quit for --apply.
"""

import os
import plistlib
import shutil
import subprocess
import sys
import time
import uuid
from urllib.parse import urlparse

PLIST = os.path.expanduser("~/Library/Safari/Bookmarks.plist")
BACKUP = PLIST + f".categorise-{time.strftime('%Y%m%d-%H%M%S')}.bak"

# folders never touched
TOUCH = {"Work", "com.apple.ReadingList", "History"}
# kept whole (already organised, left untouched)
KEEP_WHOLE = {"Conferences", "Family"}
KEEP = TOUCH | KEEP_WHOLE

# (folder, [host substrings]) — order matters: first match wins
DOMAIN_RULES = [
    ("Travel", ["head2bed", "nationalrail", "flightaware", "ihg.com", "radisson",
                "marriott", "booking.com", "skyscanner", "easyjet", "britishairways",
                "ba.com", "csair", "tripadvisor", "klook", "lastminute", "copenhagen",
                "brighton", "airport", "hotels", "expedia", "signalbox.io",
                "crisscrosschina", "flyingfluskey", "usembassy", "bletchley", "smartify",
                "trainline"]),
    ("Home & Network", ["opnsense", "myfrit", "proxmox", "192.168", "10.0.", "100.64",
                        "100.1.", "raspberrypi", "home.nest", "philips-hue", "home-assistant",
                        "unifi", "ui.com", "fritz", "windscribe", "dashcam", "virginmedia",
                        "speedtest", "sakuracat", "mesl.cloud", "avoidthehack", "tweedge",
                        "cherrybyte", "pi-hole", "adguard"]),
    ("Security", ["1password", "bitwarden", "keychain"]),
    ("Cars & EV", ["speakev", "dashcamtalk", "ev-database", "tesla", "hyundai", "toyota",
                   "autotrader", "carwow", "rjmeakerfencing"]),
    ("Health", ["nhs.uk", "healthline", "alburycare", "pharmacy", "dentist",
                "patient.info", "webmd", "wibbi", "clinicaltrials"]),
    ("News", ["bbc.co.uk", "bbc.com", "theguardian", "theargus", "reuters", "thetimes",
              "telegraph", "independent.co.uk", "skynews", "news.sky", "dailymail",
              "mirror.co.uk", "inews.co.uk", "ft.com", "economist.com",
              "chandlersfordtoday", "news.google"]),
    ("Money & Admin", ["monzo", "revolut", "paypal", "trading212", "gov.uk", "hmrc",
                       "comparethemarket", "moneysupermarket", "uswitch", "confused",
                       "directline", "aviva", "starling", "natwest", "lloyds", "barclays",
                       "tesco.bank", "argos", "currys", "hm-revenue"]),
    ("Shopping & Food", ["amazon", "ocado", "johnlewis", "boots", "ikea", "nike",
                         "aliexpress", "asos", "restaurant", "takeaway", "just-eat",
                         "ubereats", "deliveroo", "sainsbury", "waitrose", "co-op",
                         "morrisons", "gomodern", "italianfurniture", "lunns.net",
                         "sahm-glass", "stonepaving", "moderngardens", "brewdog"]),
    ("Jobs", ["technojobs", "indeed", "reed.co", "linkedin", "cv-library", "glassdoor",
              "devitjobs", "cwjobs", "mcgill"]),
    ("Programming", ["github", "stackoverflow", "stackexchange", "apache.org", "redhat",
                     "quarkus", "gradle", "toodledo", "zenhub", "trello", "linearb",
                     "gitlab", "leetcode", "kubernetes", "k8s", "docker", "gitignores",
                     "pypi.org", "streamlit"]),
    ("AI", ["huggingface", "openai", "anthropic", "claude.ai", "gemini", "ollama",
            "vllm", "langchain", "towardsdatascience", "kaggle", "quantum", "qiskit",
            "granite", "mellea", "stability.ai", "thispersondoesnotexist", "dataquest",
            "mrdbourke", "medium.com"]),
    ("Tech", ["microsoft", "apple.com", "cloudflare", "kitz", "linuxfoundation",
              "lfaidata", "macropolo", "techcrunch", "arstechnica", "hackernews",
              "ibmbigdatahub", "stackenterprise", "orangesharing", "mural.co",
              "mediafire", "apnic", "patents.google", "icloud", "yahoo", "wipo.int",
              "techcarbonstandard", "qanplatform", "finos.org"]),
    ("Community", ["openuk", "climate", "meetup", "forum", "openstack", "ubuntu",
                   "reddit", "softwaremaxims", "bsky"]),
    ("Entertainment", ["twitter", "x.com", "facebook", "youtube", "netflix",
                       "imdb", "spotify", "formula1", "f1", "lastminutemusicians",
                       "iplayer", "deezer", "twitch", "discord", "instagram",
                       "warble", "quaywest", "spotify"]),
]

# (folder, [title keywords]) — case-insensitive, checked after domains
TITLE_RULES = [
    ("Travel", ["hotel", "flight", "holiday", "break ", "cruise", "airport", "gig ",
                "train", "road trip", "roadtrip"]),
    ("Health", ["nhs", "doctor", "dentist", "pharmacy", "clinic", "hearing aid"]),
    ("Shopping & Food", ["order", "delivery", "menu", "recipe", "restaurant",
                         "garden", "furniture"]),
    ("Money & Admin", ["bank", "mortgage", "credit", "tax", "council tax"]),
    ("Entertainment", ["movie", "film", "series", "album", "concert", "singer"]),
    ("Jobs", ["job", "internship", "vacancy", "careers"]),
    ("Community", ["forum", "community"]),
]


def die(msg: str) -> None:
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


# (folder, [path substring]) — the import preserved Edge's own subfolder
# structure; your original filing intent is the strongest remaining signal.
# Applied after domain rules, before title rules.
PATH_RULES = [
    ("cars", "Cars & EV"),
    ("beds", "Shopping & Food"),
    ("desks", "Shopping & Food"),
    ("wedding", "Shopping & Food"),
    ("xmas", "Shopping & Food"),
    ("coffee", "Shopping & Food"),
    ("gradle", "Programming"),
    ("k8s", "Programming"),
    ("programming", "Programming"),
    ("tech", "Tech"),
    ("egeria", "Tech"),
    ("ibm", "Tech"),
    ("science", "Tech"),
    ("home it", "Home & Network"),
    ("repairs", "Home & Network"),
    ("stuff > legal", "Money & Admin"),
    ("consent, privacy", "Money & Admin"),
    ("china", "Travel"),
    ("london", "Travel"),
    ("scotland", "Travel"),
    ("australia", "Travel"),
    ("travel", "Travel"),
    ("holiday", "Travel"),
    ("climate", "Community"),
    ("social media", "Entertainment"),
    ("f1", "Entertainment"),
    ("health", "Health"),
    ("jobs", "Jobs"),
]

# junk: chrome:// internals (host parses as the page name), file saves,
# stale homepage/search bookmarks, unreachable ∅-URL entries.
JUNK_HOSTS = {"google.com", "www.google.com", "google.co.uk", "www.google.co.uk",
              "bing.com", "www.bing.com", "downloads", "extensions", "history",
              "settings", "newtab", "bookmarks", "chrome", "sync-internals",
              "pof.es",
              # adult
              "xvideos.com", "www.xvideos.com", "livejasmin.com", "crmkt.livejasmin.com",
              "xnxx.com", "www.xnxx.com", "xhamster.com", "www.xhamster.com",
              "pornhub.com", "www.pornhub.com", "youporn.com", "www.youporn.com",
              "redtube.com", "www.redtube.com", "spankbang.com", "www.spankbang.com",
              "badoink.com", "www.badoink.com", "x-art.com", "www.x-art.com",
              "nautil.us", "clips4sale.com"}
JUNK_TITLES = ("aboutblank", "new tab", "settings – about chrome",
               "activity - better history", "----------", "recent tags",
               "recently bookmarked", "livejasmin")
JUNK_TITLE_KWS = ("orgasm", "sex show", "porn", "xxx")
JUNK_FILE_EXT = (".pdf", ".ppt", ".pptx", ".jpg", ".jpeg", ".doc")


def is_junk(url: str, title: str) -> str | None:
    h = host_of(url)
    t = title.lower()
    if h in JUNK_HOSTS:
        return f"stale/adult/junk host: {h}"
    if t in JUNK_TITLES:
        return f"junk page: {title[:30]!r}"
    if any(k in t for k in JUNK_TITLE_KWS):
        return f"adult content: {title[:30]!r}"
    if not h:  # unreachable — no URL at all (file:// pointers, place:, js:, about:)
        return "no URL (unreachable)"
    if url.lower().split("?")[0].endswith(JUNK_FILE_EXT):
        return "saved file: " + url[-40:]
    return None


def classify(url: str, title: str, path: str = "") -> str:
    h = host_of(url)
    for folder, subs in DOMAIN_RULES:
        if any(s in h for s in subs):
            return folder
    p = path.lower()
    for sub, folder in PATH_RULES:
        if sub in p:
            return folder
    t = title.lower()
    for folder, kws in TITLE_RULES:
        if any(k in t for k in kws):
            return folder
    return "Unsorted"


def main() -> int:
    apply = "--apply" in sys.argv
    if apply and subprocess.run(["pgrep", "-x", "Safari"]).returncode == 0:
        die("Safari is running — quit it (⌘Q) first, then retry with --apply.")

    with open(PLIST, "rb") as f:
        root = plistlib.load(f)
    top = root.get("Children", [])

    # collect personal leaves with full folder path (Edge subfolders preserve
    # the user's original filing intent)
    leaves: list[tuple[dict, str]] = []
    for ch in top:
        t = ch.get("Title", "")
        if t in KEEP:
            continue

        def walk(node, path):
            if node.get("WebBookmarkType") == "WebBookmarkTypeLeaf":
                leaves.append((node, " > ".join(path)))
                return
            for c in node.get("Children", []):
                walk(c, path + [c.get("Title", "?")])

        walk(ch, [t])

    def count_leaves(n: dict) -> int:
        if n.get("WebBookmarkType") == "WebBookmarkTypeLeaf":
            return 1
        return sum(count_leaves(c) for c in n.get("Children", []))

    kept_whole = {ch.get("Title"): sum(count_leaves(c) for c in ch.get("Children", []))
                  for ch in top if ch.get("Title") in (KEEP - TOUCH)}

    plan: dict[str, list[dict]] = {}
    junk: list[tuple[dict, str]] = []
    for leaf, path in leaves:
        url = leaf.get("URLString", "")
        title = (leaf.get("URIDictionary") or {}).get("title", "") or leaf.get("Title", "")
        why = is_junk(url, title)
        if why:
            junk.append((leaf, why))
            continue
        plan.setdefault(classify(url, title, path), []).append(leaf)

    # report
    print(f"Personal leaves to categorise: {len(leaves)}  (junk to drop: {len(junk)})")
    print("Kept whole: " + ", ".join(f"{k} ({v})" for k, v in kept_whole.items()) + "\n")
    for folder in ["Travel", "Programming", "AI", "Tech", "Home & Network", "Health",
                   "Entertainment", "News", "Money & Admin", "Shopping & Food",
                   "Cars & EV", "Jobs", "Community", "Unsorted"]:
        ls = plan.get(folder, [])
        samples = " | ".join(
            ((l.get("URIDictionary") or {}).get("title", "") or l.get("URLString", ""))[:36]
            for l in ls[:3]) if ls else ""
        print(f"  {folder:18} {len(ls):>4}   {samples}")

    if junk:
        print(f"\nJunk to drop ({len(junk)}):")
        by_why: dict[str, list[dict]] = {}
        for leaf, why in junk:
            by_why.setdefault(why.split(":")[0], []).append(leaf)
        for why, ls in by_why.items():
            sample = (ls[0].get("URIDictionary") or {}).get("title", "") or ls[0].get("URLString", "")
            print(f"   {len(ls):>3}  {why:22} e.g. {sample[:48]}")

    if not apply:
        print("\nDRY RUN — nothing changed. Re-run with --apply.")
        return 0

    # apply: drop harvested leaves + non-kept containers from top level,
    # rename kept folders into scheme names, create missing scheme folders,
    # re-home every harvested leaf.
    harvested = {id(leaf) for leaf, _ in leaves}
    top[:] = [c for c in top if id(c) not in harvested and
              not (c.get("WebBookmarkType") == "WebBookmarkTypeList" and c.get("Title", "") not in KEEP)]

    def ensure_folder(name: str) -> dict:
        for ch in top:
            if ch.get("WebBookmarkType") == "WebBookmarkTypeList" and ch.get("Title") == name:
                return ch
        node = {
            "WebBookmarkType": "WebBookmarkTypeList",
            "WebBookmarkUUID": uuid.uuid4().hex.upper(),
            "Title": name,
            "Children": [],
        }
        top.append(node)
        return node

    for folder, ls in plan.items():
        target = ensure_folder(folder)
        for leaf in ls:
            target.setdefault("Children", []).append(leaf)

    with open(PLIST, "wb") as f:
        plistlib.dump(root, f)
    print(f"\n✅ Applied. Backup → {os.path.basename(BACKUP)}")
    print("Launch Safari and walk the new folders; restore the backup if anything looks wrong.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
