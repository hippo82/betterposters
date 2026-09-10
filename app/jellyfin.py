"""Jellyfin API client."""

import base64
import logging
import time

import requests

from . import config

SESSION = requests.Session()

logger = logging.getLogger(__name__)


def _headers():
    return {"Authorization": f'MediaBrowser Token="{config.API_KEY}"', "accept": "application/json"}


def get_media_items():
    """Returns the library items, or None when Jellyfin is unreachable/errors.

    When UPDATE_SEASONS is enabled, Season items are included as well and are
    given the IMDb id of their parent series so they receive the same
    btttr.cc poster (the home screen shows the season poster for series).
    """
    include = "Movie,Series,Season" if config.UPDATE_SEASONS else "Movie,Series"
    url = f"{config.SERVER_URL}/Items"
    params = {
        "IncludeItemTypes": include,
        "Fields": "ProviderIds",
        "Recursive": "true",
    }
    try:
        response = SESSION.get(url, headers=_headers(), params=params, timeout=30)
    except requests.exceptions.RequestException as e:
        print(f"Error fetching library from Jellyfin: {e}")
        return None
    if response.status_code != 200:
        print(f"Error fetching library from Jellyfin: {response.status_code}")
        return None
    items = response.json().get("Items", [])
    if config.UPDATE_SEASONS:
        _attach_series_imdb(items)
    return items


def _attach_series_imdb(items):
    """Copy the parent series IMDb id onto each Season item."""
    series_imdb = {
        item.get("Id"): (item.get("ProviderIds") or {}).get("Imdb")
        for item in items
        if item.get("Type") == "Series"
    }
    for item in items:
        if item.get("Type") != "Season":
            continue
        # "Unknown season" placeholders (no IndexNumber) reject image uploads
        if item.get("IndexNumber") is None:
            continue
        imdb = series_imdb.get(item.get("SeriesId"))
        if not imdb:
            continue
        if not item.get("ProviderIds"):
            item["ProviderIds"] = {}
        item["ProviderIds"]["Imdb"] = imdb


def fetch_fresh_tags(item_ids):
    """Fetch current ImageTags.Primary for a list of ids (batched in chunks).

    Returns None when no tags could be fetched at all (Jellyfin unreachable).
    """
    result = {}
    for i in range(0, len(item_ids), config.IDS_CHUNK):
        chunk = item_ids[i:i + config.IDS_CHUNK]
        try:
            response = SESSION.get(
                f"{config.SERVER_URL}/Items",
                params={"Ids": ",".join(chunk)},
                headers=_headers(),
                timeout=30,
            )
        except requests.exceptions.RequestException:
            continue
        if response.status_code == 200:
            for item in response.json().get("Items", []):
                result[item.get("Id")] = item.get("ImageTags", {}).get("Primary")
    if item_ids and not result:
        return None
    return result


def get_fresh_tag(item_id, previous_tag=None):
    """Read the tag after a fresh upload, waiting until it changes (Jellyfin async)."""
    for attempt in range(5):
        try:
            response = SESSION.get(
                f"{config.SERVER_URL}/Items",
                params={"Ids": item_id},
                headers=_headers(),
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


def upload_image(item_id, image_bytes):
    upload_url = f"{config.SERVER_URL}/Items/{item_id}/Images/Primary"
    upload_headers = {
        "Authorization": f'MediaBrowser Token="{config.API_KEY}"',
        "Content-Type": "image/jpeg",
    }
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    try:
        upload_response = SESSION.post(upload_url, headers=upload_headers, data=base64_image, timeout=15)
    except requests.exceptions.RequestException as e:
        logger.error("Jellyfin upload request failed for item %s: %s", item_id, e)
        return False
    if upload_response.status_code not in (200, 204):
        logger.error("Jellyfin upload for item %s returned HTTP %s: %s",
                     item_id, upload_response.status_code, upload_response.text[:200])
        return False
    return True
