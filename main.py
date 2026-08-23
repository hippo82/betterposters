"""
BetterPosters — Jellyfin poster updater from btttr.cc.

Instead of updating every poster on each run, it tracks state in a local
SQLite database (betterposters.db). Only these items are updated:
  1. new items (not present in the database),
  2. items whose poster changed at the source (btttr.cc) — detected via the
     ETag of the generated poster (user ratings / community popularity),
  3. items whose poster was changed by Jellyfin itself.

Change detection:
  - Source (btttr.cc): conditional GET with If-None-Match using the stored
    ETag; a 304 response means the poster is unchanged.
  - Jellyfin: ImageTags.Primary changes on every image change (e.g. after a
    metadata refresh by Jellyfin). NOTE: the list endpoint (/Items?Recursive)
    returns stale (cached) tags, so current tags are fetched in batches via
    /Items?Ids= (chunked), which reflects the live state.

Usage:
  python main.py                 # update only new / changed / updated posters
  python main.py --force         # update ALL posters, ignoring the database

Configuration (.env file next to the script or environment variables):
  SERVER_URL            Jellyfin address, e.g. https://your-host/jellyfin
  API_KEY               API key (X-Emby-Token)
  DB_PATH               path to the SQLite database (default: betterposters.db)
  POSTER_LANG           poster language for btttr.cc (default: en)
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
POSTER_LANG = os.getenv("POSTER_LANG", "en")
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
            item_id     TEXT PRIMARY KEY,
            imdb_id     TEXT,
            image_tag   TEXT,
            source_etag TEXT,
            updated_at  TEXT
        )
    """)
    try:
        conn.execute("ALTER TABLE posters ADD COLUMN source_etag TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
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


def fetch_poster(imdb_id, etag=None):
    """Conditional GET from btttr.cc.

    Returns (status, bytes, new_etag):
      status 'unchanged' -> 304, the generated poster has not changed
      status 'changed'   -> 200 with new bytes and (possibly) a new ETag
      status 'error'     -> HTTP error or network failure
    """
    poster_url = f"https://btttr.cc/poster/imdb/poster-default/{imdb_id}.jpg?lang={POSTER_LANG}"
    request_headers = {"If-None-Match": etag} if etag else {}
    try:
        response = requests.get(poster_url, headers=request_headers, timeout=10)
    except requests.exceptions.RequestException:
        return "error", None, None

    if response.status_code == 304:
        return "unchanged", None, etag
    if response.status_code == 200:
        return "changed", response.content, response.headers.get("ETag")
    return "error", None, None


def upload_image(item_id, image_bytes):
    upload_url = f"{SERVER_URL}/Items/{item_id}/Images/Primary"
    upload_headers = {
        "X-Emby-Token": API_KEY,
        "Content-Type": "image/jpeg",
    }
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    try:
        upload_response = requests.post(upload_url, headers=upload_headers, data=base64_image, timeout=15)
        return upload_response.status_code in (200, 204)
    except requests.exceptions.RequestException:
        return False


def save_poster(conn, item_id, imdb_id, image_tag, source_etag):
    conn.execute(
        """
        INSERT INTO posters (item_id, imdb_id, image_tag, source_etag, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            imdb_id     = excluded.imdb_id,
            image_tag   = excluded.image_tag,
            source_etag = excluded.source_etag,
            updated_at  = excluded.updated_at
        """,
        (item_id, imdb_id, image_tag, source_etag, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def run_once(force):
    conn = init_db()
    items = get_media_items()

    fresh_tags = fetch_fresh_tags([item.get("Id") for item in items])

    updated = 0
    skipped = 0
    failed = 0

    for item in items:
        item_id = item.get("Id")
        imdb_id = item.get("ProviderIds", {}).get("Imdb")
        current_tag = fresh_tags.get(item_id)

        if not imdb_id:
            continue

        row = conn.execute(
            "SELECT image_tag, source_etag FROM posters WHERE item_id = ?", (item_id,)
        ).fetchone()
        db_tag = row[0] if row else None
        db_etag = row[1] if row else None

        if force:
            status, data, new_etag = fetch_poster(imdb_id, None)
            if status == "changed" and upload_image(item_id, data):
                save_poster(conn, item_id, imdb_id, get_fresh_tag(item_id, current_tag) or current_tag, new_etag or db_etag)
                updated += 1
            else:
                failed += 1
            continue

        status, data, new_etag = fetch_poster(imdb_id, db_etag)

        if status == "error":
            failed += 1
            continue

        if status == "unchanged":
            if db_tag == current_tag:
                skipped += 1
                continue
            # Jellyfin replaced the poster -> restore the current btttr version
            status2, data2, _ = fetch_poster(imdb_id, None)
            if status2 == "changed" and upload_image(item_id, data2):
                save_poster(conn, item_id, imdb_id, get_fresh_tag(item_id, current_tag) or current_tag, db_etag)
                updated += 1
            else:
                failed += 1
            continue

        # source changed (ratings / popularity) or new item -> upload
        if upload_image(item_id, data):
            save_poster(conn, item_id, imdb_id, get_fresh_tag(item_id, current_tag) or current_tag, new_etag or db_etag)
            updated += 1
        else:
            failed += 1

    conn.close()
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
