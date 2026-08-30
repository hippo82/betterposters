"""Jellyfin API client."""

import base64
import time

import requests

import config

SESSION = requests.Session()


def _headers():
    return {"X-Emby-Token": config.API_KEY, "accept": "application/json"}


def get_media_items():
    url = f"{config.SERVER_URL}/Items"
    params = {
        "IncludeItemTypes": "Movie,Series",
        "Fields": "ProviderIds",
        "Recursive": "true",
    }
    response = SESSION.get(url, headers=_headers(), params=params, timeout=30)
    if response.status_code == 200:
        return response.json().get("Items", [])
    print(f"Error fetching library from Jellyfin: {response.status_code}")
    return []


def fetch_fresh_tags(item_ids):
    """Fetch current ImageTags.Primary for a list of ids (batched in chunks)."""
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
        "X-Emby-Token": config.API_KEY,
        "Content-Type": "image/jpeg",
    }
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    try:
        upload_response = SESSION.post(upload_url, headers=upload_headers, data=base64_image, timeout=15)
        return upload_response.status_code in (200, 204)
    except requests.exceptions.RequestException:
        return False
