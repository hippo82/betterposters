"""
BetterPosters — Jellyfin poster updater from btttr.cc.

Instead of updating every poster on each run, it tracks state in a local
SQLite database (betterposters.db). Only these items are updated:
  1. new items (not present in the database),
  2. items whose poster was changed by Jellyfin itself.

Change detection: Jellyfin items expose ImageTags.Primary — a hash of the
poster that changes on every image change (e.g. after a metadata refresh
by Jellyfin). NOTE: the list endpoint (/Items?Recursive) returns stale
(cached) tags, so current tags are fetched in batches via /Items?Ids=
(chunked), which reflects the live state.

Usage:
  python main.py                 # update only new / changed by Jellyfin
  python main.py --force         # update ALL posters, ignoring the database

Configuration (.env file next to the script or environment variables):
  SERVER_URL            Jellyfin address, e.g. https://your-host/jellyfin
  API_KEY               API key (X-Emby-Token)
  DB_PATH               path to the SQLite database (default: betterposters.db)
  RUN_INTERVAL_MINUTES  run every N minutes (0 = run once and exit)
"""

import argparse
import base64
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

# --- CONFIGURATION (from environment variables / .env) ---
SERVER_URL = os.getenv("SERVER_URL", "").rstrip("/")
API_KEY = os.getenv("API_KEY", "")
DB_PATH = os.getenv("DB_PATH", "betterposters.db")
IDS_CHUNK = 100
RUN_INTERVAL_MINUTES = int(os.getenv("RUN_INTERVAL_MINUTES", "0") or 0)

headers = {
    "X-Emby-Token": API_KEY,
    "accept": "application/json",
}


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS posters (
            item_id    TEXT PRIMARY KEY,
            imdb_id    TEXT,
            image_tag  TEXT,
            updated_at TEXT
        )
    """)
    conn.commit()
    return conn


def get_media_items():
    url = f"{SERVER_URL}/Items"
    params = {
        "IncludeItemTypes": "Movie,Series",
        "Fields": "ProviderIds",
        "Recursive": "true",
    }
    response = requests.get(url, headers=headers, params=params, timeout=30)
    if response.status_code == 200:
        return response.json().get("Items", [])
    print(f"Error fetching library from Jellyfin: {response.status_code}")
    return []


def fetch_fresh_tags(item_ids):
    """Fetch current ImageTags.Primary for a list of ids (batched in chunks)."""
    result = {}
    for i in range(0, len(item_ids), IDS_CHUNK):
        chunk = item_ids[i:i + IDS_CHUNK]
        try:
            response = requests.get(
                f"{SERVER_URL}/Items",
                params={"Ids": ",".join(chunk)},
                headers=headers,
                timeout=30,
            )
        except requests.exceptions.RequestException:
            continue
        if response.status_code == 200:
            for item in response.json().get("Items", []):
                result[item.get("Id")] = item.get("ImageTags", {}).get("Primary")
    return result


def get_fresh_tag(item_id, previous_tag=None):
    """Read the tag after a fresh upload, waiting until it changes (Jellyfin async)."""
    for attempt in range(5):
        try:
            response = requests.get(
                f"{SERVER_URL}/Items",
                params={"Ids": item_id},
                headers=headers,
                timeout=20,
            )
        except requests.exceptions.RequestException:
            time.sleep(1)
            continue
        if response.status_code == 200:
            items = response.json().get("Items", [])
            if items:
                tag = items[0].get("ImageTags", {}).get("Primary")
                if tag and tag != previous_tag:
                    return tag
        time.sleep(1)
    return None


def upload_poster(item_id, item_name, imdb_id):
    poster_url = f"https://btttr.cc/poster/imdb/poster-default/{imdb_id}.jpg?lang=pl"

    print(f"Downloading poster for '{item_name}' ({imdb_id})...")

    try:
        poster_response = requests.get(poster_url, timeout=10)
        if poster_response.status_code != 200:
            print(f"  -> Skipped: file not found on btttr.cc (HTTP {poster_response.status_code})")
            return False
    except requests.exceptions.RequestException as e:
        print(f"  -> Network error while downloading from btttr.cc: {e}")
        return False

    base64_image = base64.b64encode(poster_response.content).decode('utf-8')

    upload_url = f"{SERVER_URL}/Items/{item_id}/Images/Primary"
    upload_headers = {
        "X-Emby-Token": API_KEY,
        "Content-Type": "image/jpeg",
    }

    try:
        upload_response = requests.post(upload_url, headers=upload_headers, data=base64_image, timeout=15)
        if upload_response.status_code in (200, 204):
            print("  -> Success! Poster updated.")
            return True
        print(f"  -> Jellyfin error: {upload_response.status_code} - {upload_response.text}")
    except requests.exceptions.RequestException as e:
        print(f"  -> Connection error while uploading to Jellyfin: {e}")
    return False


def save_poster(conn, item_id, imdb_id, image_tag):
    conn.execute(
        """
        INSERT INTO posters (item_id, imdb_id, image_tag, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            imdb_id    = excluded.imdb_id,
            image_tag  = excluded.image_tag,
            updated_at = excluded.updated_at
        """,
        (item_id, imdb_id, image_tag, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def run_once(force):
    conn = init_db()
    items = get_media_items()
    print(f"Found {len(items)} items in the library.")

    fresh_tags = fetch_fresh_tags([item.get("Id") for item in items])
    print(f"Fetched current poster tags: {len(fresh_tags)}.")

    updated = 0
    skipped = 0
    failed = 0

    for item in items:
        item_id = item.get("Id")
        item_name = item.get("Name")
        imdb_id = item.get("ProviderIds", {}).get("Imdb")
        current_tag = fresh_tags.get(item_id)

        if not imdb_id:
            print(f"Skipped '{item_name}': no IMDb ID assigned.")
            continue

        row = conn.execute(
            "SELECT image_tag FROM posters WHERE item_id = ?", (item_id,)
        ).fetchone()

        if not force and row and row[0] == current_tag:
            print(f"Skipped '{item_name}': already updated.")
            skipped += 1
            continue

        if force:
            print(f"Forced update (--force) for '{item_name}'.")
        elif row and row[0] != current_tag:
            if current_tag:
                print(f"Detected poster change by Jellyfin for '{item_name}' — updating again.")
            else:
                print(f"'{item_name}': poster missing — updating again.")

        if upload_poster(item_id, item_name, imdb_id):
            new_tag = get_fresh_tag(item_id, current_tag)
            if new_tag is None:
                new_tag = current_tag
            save_poster(conn, item_id, imdb_id, new_tag)
            updated += 1
        else:
            failed += 1

    conn.close()
    print("=" * 50)
    print(f"Summary: updated {updated}, skipped {skipped}, errors {failed}.")
    return failed


def main():
    print("BetterPosters — Jellyfin poster updater")
    print("=" * 50)

    if not SERVER_URL or not API_KEY:
        print("Error: SERVER_URL and/or API_KEY are not configured. Fill in the .env file.")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="BetterPosters")
    parser.add_argument(
        "--force",
        action="store_true",
        help="update all posters, ignoring the state database",
    )
    args = parser.parse_args()

    if RUN_INTERVAL_MINUTES > 0:
        print(f"Continuous mode: update every {RUN_INTERVAL_MINUTES} min.")

    while True:
        run_once(args.force)

        if RUN_INTERVAL_MINUTES <= 0:
            break

        print(f"Next run in {RUN_INTERVAL_MINUTES} min...")
        time.sleep(RUN_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()
