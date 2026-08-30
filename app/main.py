"""
BetterPosters — Jellyfin poster updater from btttr.cc.

Instead of updating every poster on each run, it tracks state in a local
SQLite database (db.py). Only these items are updated:
  1. new items (not present in the database),
  2. items whose poster changed at the source (btttr.cc) — detected via the
     ETag of the generated poster (user ratings / community popularity),
  3. items whose poster was changed by Jellyfin itself.

Modules (app package):
  config.py    environment configuration
  jellyfin.py  Jellyfin API client
  btttr.py     btttr.cc poster source (ETag conditional GET)
  db.py        SQLite persistence
  api.py       optional REST API (loaded only when API_PORT is set)

Usage:
  python main.py                 # update only new / changed / updated posters
  python main.py --force         # update ALL posters, ignoring the database
"""

import argparse
import sys
import threading
import time
from datetime import datetime, timezone

from . import btttr, config, db, jellyfin

START_TIME = time.monotonic()
sync_lock = threading.Lock()
last_result = {
    "updated": None,
    "skipped": None,
    "errors": None,
    "last_run": None,
    "uptime_seconds": None,
}


def run_once(force, reason="scheduled"):
    print(f"Update started ({reason})...")
    conn = db.init_db()
    items = jellyfin.get_media_items()

    fresh_tags = jellyfin.fetch_fresh_tags([item.get("Id") for item in items])

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
            status, data, new_etag = btttr.fetch_poster(imdb_id, None)
            if status == "changed" and jellyfin.upload_image(item_id, data):
                db.save_poster(conn, item_id, imdb_id,
                               jellyfin.get_fresh_tag(item_id, current_tag) or current_tag,
                               new_etag or db_etag)
                updated += 1
            else:
                failed += 1
            continue

        status, data, new_etag = btttr.fetch_poster(imdb_id, db_etag)

        if status == "error":
            failed += 1
            continue

        if status == "unchanged":
            if db_tag == current_tag:
                skipped += 1
                continue
            # Jellyfin replaced the poster -> restore the current btttr version
            status2, data2, _ = btttr.fetch_poster(imdb_id, None)
            if status2 == "changed" and jellyfin.upload_image(item_id, data2):
                db.save_poster(conn, item_id, imdb_id,
                               jellyfin.get_fresh_tag(item_id, current_tag) or current_tag,
                               db_etag)
                updated += 1
            else:
                failed += 1
            continue

        # source changed (ratings / popularity) or new item -> upload
        if jellyfin.upload_image(item_id, data):
            db.save_poster(conn, item_id, imdb_id,
                           jellyfin.get_fresh_tag(item_id, current_tag) or current_tag,
                           new_etag or db_etag)
            updated += 1
        else:
            failed += 1

    conn.close()
    print(f"Summary: updated {updated}, skipped {skipped}, errors {failed}.")
    print(f"Update finished ({reason}).")
    last_result.clear()
    last_result.update({
        "updated": updated,
        "skipped": skipped,
        "errors": failed,
        "last_run": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": int(time.monotonic() - START_TIME),
    })
    return failed


def main():
    print("BetterPosters — Jellyfin poster updater")
    print("=" * 50)

    if not config.SERVER_URL or not config.API_KEY:
        print("Error: SERVER_URL and/or API_KEY are not configured. Fill in the .env file.")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="BetterPosters")
    parser.add_argument(
        "--force",
        action="store_true",
        help="update all posters, ignoring the state database",
    )
    args = parser.parse_args()

    if config.RUN_INTERVAL_MINUTES > 0:
        print(f"Continuous mode: update every {config.RUN_INTERVAL_MINUTES} min.")
        if config.API_PORT:
            # load the API module lazily, only when an API port is configured
            from .api import ApiDeps, start_http_server
            try:
                start_http_server(ApiDeps(
                    run_once=run_once,
                    sync_lock=sync_lock,
                    last_result=last_result,
                    start_time=START_TIME,
                ), port=int(config.API_PORT))
            except OSError as e:
                print(f"Warning: could not start API server: {e}")

    while True:
        reason = "force" if args.force else "scheduled"
        with sync_lock:
            run_once(args.force, reason=reason)

        if config.RUN_INTERVAL_MINUTES <= 0:
            break

        print(f"Next run in {config.RUN_INTERVAL_MINUTES} min...")
        time.sleep(config.RUN_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()
