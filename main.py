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

REST API (continuous mode only):
  API_PORT              HTTP port for the API (default: 8080)
  API_TOKEN             token required by POST /refresh (X-API-Token / Bearer)
  API_RATE_LIMIT        max /refresh requests per minute per client (default: 5)
  API_GLOBAL_REFRESH_LIMIT  max /refresh requests per minute globally (default: 20)
  API_STATUS_RATE_LIMIT     max /status requests per minute per client (default: 30)
  AUTH_FAIL_LIMIT           failed token attempts before lockout (default: 5)
  AUTH_LOCKOUT_MINUTES      lockout window for failed attempts (default: 10)

Endpoints:
  GET  /health          Docker healthcheck; loopback (127.0.0.1) only, no token
  GET  /status          last run summary (requires token, rate limited)
  POST /refresh         trigger an ETag-based update (requires token, rate limited)
"""

import argparse
import base64
import hmac
import http.server
import json
import os
import sqlite3
import sys
import threading
import time
from collections import deque
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

API_PORT = int(os.getenv("API_PORT", "8080"))
API_TOKEN = os.getenv("API_TOKEN", "")
API_RATE_LIMIT = int(os.getenv("API_RATE_LIMIT", "5") or 5)
API_GLOBAL_REFRESH_LIMIT = int(os.getenv("API_GLOBAL_REFRESH_LIMIT", "20") or 20)
API_STATUS_RATE_LIMIT = int(os.getenv("API_STATUS_RATE_LIMIT", "30") or 30)
AUTH_FAIL_LIMIT = int(os.getenv("AUTH_FAIL_LIMIT", "5") or 5)
AUTH_LOCKOUT_MINUTES = int(os.getenv("AUTH_LOCKOUT_MINUTES", "10") or 10)

headers = {
    "X-Emby-Token": API_KEY,
    "accept": "application/json",
}

# --- shared state for the HTTP API ---
START_TIME = time.monotonic()
sync_lock = threading.Lock()
last_result = {
    "updated": None,
    "skipped": None,
    "errors": None,
    "last_run": None,
    "uptime_seconds": None,
}
_rate_lock = threading.Lock()
# client key -> deque of request timestamps (sliding window)
_refresh_hits = {}
_status_hits = {}
_refresh_global = {}
_auth_failures = {}
_STORE_MAX_KEYS = 5000


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


def run_once(force, reason="scheduled"):
    print(f"Update started ({reason})...")
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
    print(f"Update finished ({reason}).")
    global last_result
    last_result = {
        "updated": updated,
        "skipped": skipped,
        "errors": failed,
        "last_run": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": int(time.monotonic() - START_TIME),
    }
    return failed


def _rate_limited(key, store, limit, window=60.0):
    """Sliding-window rate limit. Returns True when the request should be denied."""
    now = time.monotonic()
    with _rate_lock:
        dq = store.get(key)
        if dq is None:
            dq = deque()
            store[key] = dq
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= limit:
            return True
        dq.append(now)
        # keep the store bounded: drop keys with no recent activity
        if len(store) > _STORE_MAX_KEYS:
            for k in [k for k, q in store.items() if not q or now - q[-1] > window]:
                store.pop(k, None)
        return False


def _register_auth_failure(key):
    """Record a failed token attempt; returns True once the lockout kicks in."""
    now = time.monotonic()
    window = AUTH_LOCKOUT_MINUTES * 60.0
    with _rate_lock:
        dq = _auth_failures.setdefault(key, deque())
        while dq and now - dq[0] > window:
            dq.popleft()
        dq.append(now)
        return len(dq) >= AUTH_FAIL_LIMIT


def _clear_auth_failures(key):
    with _rate_lock:
        _auth_failures.pop(key, None)


class ApiHandler(http.server.BaseHTTPRequestHandler):
    server_version = "BetterPosters/1.0"

    def log_message(self, fmt, *args):
        # keep healthcheck polling out of the logs
        if self.path.split("?")[0] not in ("/health", "/api/health"):
            sys.stderr.write("api %s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, payload, retry_after=None, extra_headers=None):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _client_key(self):
        xff = self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0]

    def _is_loopback(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _token_ok(self):
        token = self.headers.get("X-API-Token") or ""
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        if not API_TOKEN or not token:
            return False
        # constant-time comparison to avoid timing attacks
        return hmac.compare_digest(token, API_TOKEN)

    def _auth_gate(self):
        """Authenticate + failed-attempt lockout. Returns True when authorized."""
        key = self._client_key()
        if not API_TOKEN:
            self._send(503, {"error": "API token not configured"})
            return False
        if self._token_ok():
            _clear_auth_failures(key)
            return True
        if _register_auth_failure(key):
            self._send(
                403, {"error": "too many failed attempts"},
                retry_after=AUTH_LOCKOUT_MINUTES * 60,
            )
        else:
            self._send(401, {"error": "unauthorized"})
        return False

    def _method_not_allowed(self):
        self._send(405, {"error": "method not allowed"}, extra_headers={"Allow": "GET, POST"})

    def _handle_health(self):
        if self._is_loopback():
            self._send(200, {"status": "ok", "uptime_seconds": int(time.monotonic() - START_TIME)})
        else:
            self._send(403, {"error": "healthcheck is loopback only"})

    def _handle_status(self):
        if not self._auth_gate():
            return
        if _rate_limited(self._client_key(), _status_hits, API_STATUS_RATE_LIMIT):
            self._send(429, {"error": "rate limit exceeded"}, retry_after=60)
            return
        self._send(200, last_result)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/health", "/api/health"):
            self._handle_health()
        elif path in ("/status", "/api/status"):
            self._handle_status()
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path in ("/refresh", "/api/refresh"):
            self._handle_refresh()
        elif path in ("/status", "/api/status"):
            self._handle_status()
        elif path in ("/health", "/api/health"):
            self._handle_health()
        else:
            self._send(404, {"error": "not found"})

    def _handle_refresh(self):
        if not self._auth_gate():
            return
        if _rate_limited(self._client_key(), _refresh_hits, API_RATE_LIMIT):
            self._send(429, {"error": "rate limit exceeded"}, retry_after=60)
            return
        if _rate_limited("__global__", _refresh_global, API_GLOBAL_REFRESH_LIMIT):
            self._send(429, {"error": "global rate limit exceeded"}, retry_after=60)
            return
        if not sync_lock.acquire(blocking=False):
            # an update is already running and will refresh posters anyway
            print(f"API: refresh requested by {self._client_key()}, an update is already running")
            self._send(202, {"status": "already running"})
            return

        def worker():
            try:
                # ETag-based update (same as the scheduled runs), not --force
                run_once(force=False, reason="api")
            finally:
                sync_lock.release()

        print(f"API: poster refresh triggered by {self._client_key()}")
        threading.Thread(target=worker, daemon=True).start()
        self._send(202, {"status": "refresh started"})

    # reject unsupported methods explicitly
    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = do_TRACE = _method_not_allowed


def start_http_server():
    server = http.server.ThreadingHTTPServer(("0.0.0.0", API_PORT), ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"API listening on :{API_PORT}  "
          f"(health: loopback only | status/refresh: token + rate limit)")


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
        try:
            start_http_server()
        except OSError as e:
            print(f"Warning: could not start API server: {e}")

    while True:
        reason = "force" if args.force else "scheduled"
        with sync_lock:
            run_once(args.force, reason=reason)

        if RUN_INTERVAL_MINUTES <= 0:
            break

        print(f"Next run in {RUN_INTERVAL_MINUTES} min...")
        time.sleep(RUN_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()
