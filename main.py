import base64
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

# --- KONFIGURACJA (ze zmiennych środowiskowych / .env) ---
SERVER_URL = os.getenv("SERVER_URL", "").rstrip("/")
API_KEY = os.getenv("API_KEY", "")

headers = {
    "X-Emby-Token": API_KEY,
    "accept": "application/json"
}


def get_media_items():
    url = f"{SERVER_URL}/Items"
    params = {
        "IncludeItemTypes": "Movie,Series",
        "Fields": "ProviderIds",
        "Recursive": "true"
    }
    response = requests.get(url, headers=headers, params=params)
    if response.status_code == 200:
        return response.json().get("Items", [])
    print(f"Błąd pobierania listy z Jellyfin: {response.status_code}")
    return []


def upload_poster(item_id, item_name, imdb_id):
    # POPRAWIONY URL: Dokładnie taki, jaki podałeś w zapytaniu
    poster_url = f"https://btttr.cc/poster/imdb/poster-default/{imdb_id}.jpg?lang=pl"

    print(f"Pobieranie okładki dla '{item_name}' ({imdb_id})...")

    try:
        poster_response = requests.get(poster_url, timeout=10)
        if poster_response.status_code != 200:
            print(f"  -> Pominięto: Brak pliku na serwerze btttr.cc (Kod HTTP: {poster_response.status_code})")
            return
    except requests.exceptions.RequestException as e:
        print(f"  -> Błąd sieciowy podczas pobierania z btttr.cc: {e}")
        return

    # Konwersja obrazu do Base64 dla API Jellyfin
    base64_image = base64.b64encode(poster_response.content).decode('utf-8')

    upload_url = f"{SERVER_URL}/Items/{item_id}/Images/Primary"

    upload_headers = {
        "X-Emby-Token": API_KEY,
        "Content-Type": "image/jpeg"
    }

    try:
        upload_response = requests.post(upload_url, headers=upload_headers, data=base64_image, timeout=15)

        # POPRAWIONY WARUNEK: Całkowicie usunięte wadliwe "in"
        if upload_response.status_code == 200 or upload_response.status_code == 204:
            print(f"  -> Sukces! Okładka została zaktualizowana.")
        else:
            print(f"  -> Błąd Jellyfin: {upload_response.status_code} - {upload_response.text}")
    except requests.exceptions.RequestException as e:
        print(f"  -> Błąd połączenia z Jellyfin podczas wysyłania: {e}")


def main():
    if not SERVER_URL or not API_KEY:
        print("Błąd: brak konfiguracji SERVER_URL i/lub API_KEY. Uzupełnij plik .env.")
        sys.exit(1)

    items = get_media_items()
    print(f"Znaleziono {len(items)} elementów w bibliotece.")

    for item in items:
        item_id = item.get("Id")
        item_name = item.get("Name")
        imdb_id = item.get("ProviderIds", {}).get("Imdb")

        if imdb_id:
            upload_poster(item_id, item_name, imdb_id)
        else:
            print(f"Pominięto '{item_name}': Brak przypisanego IMDb ID.")
if __name__ == "__main__":
    main()