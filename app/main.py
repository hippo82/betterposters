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
import fcntl
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

from . import btttr, config, db, jellyfin

START_TIME = time.monotonic()
sync_lock = threading.Lock()
shutdown_event = threading.Event()
_lock_fd = None

last_result = {
    "updated": None,
    "skipped": None,
    "errors": None,
    "updated_movies": None,
    "skipped_movies": None,
    "errors_movies": None,
    "updated_series": None,
    "skipped_series": None,
    "errors_series": None,
    "last_run": None,
    "next_run_at": None,
    "duration_seconds": None,
    "uptime_seconds": None,
}


def run_once(force, reason="scheduled"):
    started = time.monotonic()
    print(f"Update started ({reason})...")
    conn = db.init_db()
    items = jellyfin.get_media_items()

    fresh_tags = jellyfin.fetch_fresh_tags([item.get("Id") for item in items])

    rows = {
        item_id: (tag, etag)
        for item_id, tag, etag in conn.execute(
            "SELECT item_id, image_tag, source_etag FROM posters"
        )
    }

    # --- phase 1: parallel btttr.cc ETag checks ---
    tasks = []
    for item in items:
        item_id = item.get("Id")
        imdb_id = item.get("ProviderIds", {}).get("Imdb")
        if not imdb_id:
            continue
        db_tag, db_etag = rows.get(item_id, (None, None))
        tasks.append((item, imdb_id, db_tag, db_etag))

    def check(task):
        item, imdb_id, db_tag, db_etag = task
        if force:
            status, data, etag = btttr.fetch_poster(imdb_id, None)
        else:
            status, data, etag = btttr.fetch_poster(imdb_id, db_etag)
        return item, imdb_id, db_tag, db_etag, status, data, etag

    with ThreadPoolExecutor(max_workers=config.CHECK_WORKERS) as pool:
        results = list(pool.map(check, tasks))

    # --- phase 2: sequential processing (uploads + DB writes) ---
    by_type = {
        "Movie": {"updated": 0, "skipped": 0, "errors": 0},
        "Series": {"updated": 0, "skipped": 0, "errors": 0},
    }
    updated = skipped = failed = 0

    def bump(itype, key):
        nonlocal updated, skipped, failed
        by_type.setdefault(itype, {"updated": 0, "skipped": 0, "errors": 0})[key] += 1
        if key == "updated":
            updated += 1
        elif key == "skipped":
            skipped += 1
        else:
            failed += 1

    for item, imdb_id, db_tag, db_etag, status, data, new_etag in results:
        if shutdown_event.is_set():
            print("Shutdown requested, stopping between items.")
            break
        item_id = item.get("Id")
        current_tag = fresh_tags.get(item_id)
        itype = item.get("Type") or "unknown"

        if force:
            if status == "changed" and jellyfin.upload_image(item_id, data):
                db.save_poster(conn, item_id, imdb_id,
                               jellyfin.get_fresh_tag(item_id, current_tag) or current_tag,
                               new_etag or db_etag)
                bump(itype, "updated")
            else:
                bump(itype, "errors")
            continue

        if status == "error":
            bump(itype, "errors")
            continue

        if status == "unchanged":
            if db_tag == current_tag:
                bump(itype, "skipped")
                continue
            # Jellyfin replaced the poster -> restore the current btttr version
            status2, data2, _ = btttr.fetch_poster(imdb_id, None)
            if status2 == "changed" and jellyfin.upload_image(item_id, data2):
                db.save_poster(conn, item_id, imdb_id,
                               jellyfin.get_fresh_tag(item_id, current_tag) or current_tag,
                               db_etag)
                bump(itype, "updated")
            else:
                bump(itype, "errors")
            continue

        # source changed (ratings / popularity) or new item -> upload
        if jellyfin.upload_image(item_id, data):
            db.save_poster(conn, item_id, imdb_id,
                           jellyfin.get_fresh_tag(item_id, current_tag) or current_tag,
                           new_etag or db_etag)
            bump(itype, "updated")
        else:
            bump(itype, "errors")

    # --- prune DB rows for items no longer in the library ---
    known_ids = {item.get("Id") for item in items}
    db_ids = {row[0] for row in conn.execute("SELECT item_id FROM posters")}
    stale = db_ids - known_ids
    for sid in stale:
        conn.execute("DELETE FROM posters WHERE item_id = ?", (sid,))
    if stale:
        conn.commit()
        print(f"Pruned {len(stale)} stale row(s) (items no longer in the library).")

    conn.close()
    print(f"Summary: updated {updated}, skipped {skipped}, errors {failed}.")
    print(f"Update finished ({reason}).")

    now = datetime.now(timezone.utc)
    last_result.clear()
    last_result.update({
        "updated": updated,
        "skipped": skipped,
        "errors": failed,
        "updated_movies": by_type["Movie"]["updated"],
        "skipped_movies": by_type["Movie"]["skipped"],
        "errors_movies": by_type["Movie"]["errors"],
        "updated_series": by_type["Series"]["updated"],
        "skipped_series": by_type["Series"]["skipped"],
        "errors_series": by_type["Series"]["errors"],
        "last_run": now.isoformat(),
        "next_run_at": (now + timedelta(minutes=config.RUN_INTERVAL_MINUTES)).isoformat()
        if config.RUN_INTERVAL_MINUTES > 0 else None,
        "duration_seconds": int(time.monotonic() - started),
        "uptime_seconds": int(time.monotonic() - START_TIME),
    })
    return failed


def _handle_signal(signum, frame):
    print("Shutdown requested...")
    shutdown_event.set()


def _acquire_file_lock():
    """Prevent a second instance from running against the same database."""
    global _lock_fd
    lock_path = config.DB_PATH + ".lock"
    _lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another instance is already running (lock held). Exiting.")
        sys.exit(1)
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()


def main():
    print("BetterPosters — Jellyfin poster updater")
    print("=" * 50)

    if not config.SERVER_URL or not config.API_KEY:
        print("Error: SERVER_URL and/or API_KEY are not configured. Fill in the .env file.")
        sys.exit(1)

    _acquire_file_lock()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

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

    while not shutdown_event.is_set():
        reason = "force" if args.force else "scheduled"
        with sync_lock:
            run_once(args.force, reason=reason)

        if config.RUN_INTERVAL_MINUTES <= 0:
            break

        deadline = time.monotonic() + config.RUN_INTERVAL_MINUTES * 60
        print(f"Next run in {config.RUN_INTERVAL_MINUTES} min...")
        while not shutdown_event.is_set() and time.monotonic() < deadline:
            time.sleep(1)

    print("Shutdown complete.")


if __name__ == "__main__":
    main()
