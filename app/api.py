"""HTTP API for BetterPosters.

Loaded lazily by main.py, only when API_PORT is set in the environment.

Endpoints:
  GET  /health          Docker healthcheck; loopback (127.0.0.1) only, no token
  GET  /status          last run summary (requires token, rate limited)
  GET  /metrics         Prometheus metrics (requires token, rate limited)
  POST /refresh         trigger an ETag-based update (requires token, rate limited)

Configuration (in config.py):
  API_PORT              HTTP port; empty string disables the API
  API_TOKEN             token required by POST /refresh (X-API-Token / Bearer / ?token=)
  API_RATE_LIMIT        max /refresh per minute per client
  API_GLOBAL_REFRESH_LIMIT  max /refresh per minute globally
  API_STATUS_RATE_LIMIT     max /status per minute per client
  AUTH_FAIL_LIMIT           failed token attempts before lockout
  AUTH_LOCKOUT_MINUTES      lockout window for failed attempts

The token can be supplied via the X-API-Token header, Authorization: Bearer,
or the ?token= query parameter. Note: a token in the URL ends up in access
logs, so prefer the header for public deployments.
"""

import hmac
import http.server
import json
import sys
import threading
import time
from collections import deque
from datetime import datetime
from urllib.parse import parse_qs

from . import config


class ApiDeps:
    """Dependencies injected by main.py (avoids circular imports)."""

    def __init__(self, run_once, sync_lock, last_result, start_time):
        self.run_once = run_once
        self.sync_lock = sync_lock
        self.last_result = last_result
        self.start_time = start_time


_rate_lock = threading.Lock()
# client key -> deque of request timestamps (sliding window)
_refresh_hits = {}
_status_hits = {}
_refresh_global = {}
_auth_failures = {}
_STORE_MAX_KEYS = 5000


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
    window = config.AUTH_LOCKOUT_MINUTES * 60.0
    with _rate_lock:
        dq = _auth_failures.setdefault(key, deque())
        while dq and now - dq[0] > window:
            dq.popleft()
        dq.append(now)
        return len(dq) >= config.AUTH_FAIL_LIMIT


def _clear_auth_failures(key):
    with _rate_lock:
        _auth_failures.pop(key, None)


def _iso_to_ts(iso):
    if not iso:
        return 0
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return 0


def _metrics_text(result, start_time):
    """Prometheus text exposition of the last run."""
    g = "betterposters_{name} {value}\n"
    family = (
        "# HELP betterposters_{name} Posters {what} in the last run.\n"
        "# TYPE betterposters_{name} gauge\n"
    )
    out = []
    for name, what in (("updated", "updated"), ("skipped", "skipped"), ("errors", "errored")):
        out.append(family.format(name=name, what=what))
        out.append(g.format(name=name, value=result.get(name) or 0))
        out.append(g.format(name=name + '{type="movie"}', value=result.get(name + "_movies") or 0))
        out.append(g.format(name=name + '{type="series"}', value=result.get(name + "_series") or 0))
    out.append(family.format(name="last_run_timestamp_seconds", what="updated"))
    out.append(g.format(name="last_run_timestamp_seconds", value=_iso_to_ts(result.get("last_run"))))
    out.append(g.format(name="next_run_timestamp_seconds", value=_iso_to_ts(result.get("next_run_at"))))
    out.append(g.format(name="duration_seconds", value=result.get("duration_seconds") or 0))
    out.append(g.format(name="uptime_seconds", value=int(time.monotonic() - start_time)))
    return "".join(out)


class ApiHandler(http.server.BaseHTTPRequestHandler):
    server_version = "BetterPosters/1.0"

    @property
    def deps(self):
        return self.server.deps

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

    def _send_text(self, code, text):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
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
        if not token and "?" in self.path:
            token = (parse_qs(self.path.split("?", 1)[1]).get("token") or [""])[0]
        if not config.API_TOKEN or not token:
            return False
        # constant-time comparison to avoid timing attacks
        return hmac.compare_digest(token, config.API_TOKEN)

    def _auth_gate(self):
        """Authenticate + failed-attempt lockout. Returns True when authorized."""
        key = self._client_key()
        if not config.API_TOKEN:
            self._send(503, {"error": "API token not configured"})
            return False
        if self._token_ok():
            _clear_auth_failures(key)
            return True
        if _register_auth_failure(key):
            self._send(
                403, {"error": "too many failed attempts"},
                retry_after=config.AUTH_LOCKOUT_MINUTES * 60,
            )
        else:
            self._send(401, {"error": "unauthorized"})
        return False

    def _method_not_allowed(self):
        self._send(405, {"error": "method not allowed"}, extra_headers={"Allow": "GET, POST"})

    def _handle_health(self):
        if self._is_loopback():
            self._send(200, {"status": "ok", "uptime_seconds": int(time.monotonic() - self.deps.start_time)})
        else:
            self._send(403, {"error": "healthcheck is loopback only"})

    def _handle_status(self):
        if not self._auth_gate():
            return
        if _rate_limited(self._client_key(), _status_hits, config.API_STATUS_RATE_LIMIT):
            self._send(429, {"error": "rate limit exceeded"}, retry_after=60)
            return
        self._send(200, self.deps.last_result)

    def _handle_metrics(self):
        if not self._auth_gate():
            return
        if _rate_limited(self._client_key(), _status_hits, config.API_STATUS_RATE_LIMIT):
            self._send(429, {"error": "rate limit exceeded"}, retry_after=60)
            return
        self._send_text(200, _metrics_text(self.deps.last_result, self.deps.start_time))

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/health", "/api/health"):
            self._handle_health()
        elif path in ("/status", "/api/status"):
            self._handle_status()
        elif path in ("/metrics", "/api/metrics"):
            self._handle_metrics()
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path in ("/refresh", "/api/refresh"):
            self._handle_refresh()
        elif path in ("/status", "/api/status"):
            self._handle_status()
        elif path in ("/metrics", "/api/metrics"):
            self._handle_metrics()
        elif path in ("/health", "/api/health"):
            self._handle_health()
        else:
            self._send(404, {"error": "not found"})

    def _handle_refresh(self):
        if not self._auth_gate():
            return
        if _rate_limited(self._client_key(), _refresh_hits, config.API_RATE_LIMIT):
            self._send(429, {"error": "rate limit exceeded"}, retry_after=60)
            return
        if _rate_limited("__global__", _refresh_global, config.API_GLOBAL_REFRESH_LIMIT):
            self._send(429, {"error": "global rate limit exceeded"}, retry_after=60)
            return
        if not self.deps.sync_lock.acquire(blocking=False):
            # an update is already running and will refresh posters anyway
            print(f"API: refresh requested by {self._client_key()}, an update is already running")
            self._send(202, {"status": "already running"})
            return

        def worker():
            try:
                # ETag-based update (same as the scheduled runs), not --force
                self.deps.run_once(force=False, reason="api")
            finally:
                self.deps.sync_lock.release()

        print(f"API: poster refresh triggered by {self._client_key()}")
        threading.Thread(target=worker, daemon=True).start()
        self._send(202, {"status": "refresh started"})

    # reject unsupported methods explicitly
    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = do_TRACE = _method_not_allowed


def start_http_server(deps, port=None):
    port = port or int(config.API_PORT)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), ApiHandler)
    server.deps = deps
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"API listening on :{port}  "
          f"(health: loopback only | status/refresh: token + rate limit)")
