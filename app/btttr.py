"""btttr.cc poster source client."""

import requests

from . import config

SESSION = requests.Session()


def fetch_poster(imdb_id, etag=None):
    """Conditional GET from btttr.cc.

    Returns (status, bytes, new_etag):
      status 'unchanged' -> 304, the generated poster has not changed
      status 'changed'   -> 200 with new bytes and (possibly) a new ETag
      status 'error'     -> HTTP error or network failure
    """
    poster_url = f"https://btttr.cc/poster/imdb/poster-default/{imdb_id}.jpg?lang={config.POSTER_LANG}"
    request_headers = {"If-None-Match": etag} if etag else {}
    try:
        response = SESSION.get(poster_url, headers=request_headers, timeout=10)
    except requests.exceptions.RequestException:
        return "error", None, None

    if response.status_code == 304:
        return "unchanged", None, etag
    if response.status_code == 200:
        return "changed", response.content, response.headers.get("ETag")
    return "error", None, None
