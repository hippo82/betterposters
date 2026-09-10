"""btttr.cc poster source client."""

import logging
import threading

import requests

from . import config

# thread-local sessions so parallel checks each reuse their own connection pool
_tls = threading.local()

logger = logging.getLogger(__name__)

# per-run cache: (imdb_id, etag) -> result, so a series and its seasons share
# one request (and a missing poster is reported once)
_cache = {}
_cache_lock = threading.Lock()
_key_locks = {}
_key_locks_lock = threading.Lock()


def clear_cache():
    """Drop cached responses; called at the start of each run."""
    with _cache_lock:
        _cache.clear()
    with _key_locks_lock:
        _key_locks.clear()


def _key_lock(key):
    with _key_locks_lock:
        lock = _key_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _key_locks[key] = lock
        return lock


def _session():
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        _tls.session = s
    return s


def fetch_poster(imdb_id, etag=None):
    """Conditional GET from btttr.cc.

    Returns (status, bytes, new_etag):
      status 'unchanged' -> 304, the generated poster has not changed
      status 'changed'   -> 200 with new bytes and (possibly) a new ETag
      status 'error'     -> HTTP error or network failure
    """
    key = (imdb_id, etag)
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    # serialise concurrent requests for the same key (parallel ETag checks)
    with _key_lock(key):
        with _cache_lock:
            if key in _cache:
                return _cache[key]
        result = _fetch_poster(imdb_id, etag)
        with _cache_lock:
            _cache[key] = result
        return result


def _fetch_poster(imdb_id, etag):
    poster_url = f"https://btttr.cc/poster/imdb/poster-default/{imdb_id}.jpg?lang={config.POSTER_LANG}"
    request_headers = {"If-None-Match": etag} if etag else {}
    try:
        response = _session().get(poster_url, headers=request_headers, timeout=10)
    except requests.exceptions.RequestException as e:
        logger.error("btttr.cc request failed for %s: %s", imdb_id, e)
        return "error", None, None

    if response.status_code == 304:
        return "unchanged", None, etag
    if response.status_code == 200:
        return "changed", response.content, response.headers.get("ETag")
    if response.status_code == 404:
        logger.error("btttr.cc has no poster for %s (HTTP 404): %s", imdb_id, poster_url)
    else:
        logger.error("btttr.cc returned HTTP %s for %s: %s",
                     response.status_code, imdb_id, poster_url)
    return "error", None, None
