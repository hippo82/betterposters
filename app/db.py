"""SQLite persistence."""

import sqlite3
from datetime import datetime, timezone

from . import config


def init_db():
    conn = sqlite3.connect(config.DB_PATH)
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_stats (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint   TEXT,
            method     TEXT,
            status     INTEGER,
            client     TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    return conn


def log_api_request(endpoint, method, status, client):
    """Best-effort record of an API request (never raises)."""
    try:
        conn = sqlite3.connect(config.DB_PATH, timeout=10)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_stats (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    endpoint   TEXT,
                    method     TEXT,
                    status     INTEGER,
                    client     TEXT,
                    created_at TEXT
                )
            """)
            conn.execute(
                "INSERT INTO api_stats (endpoint, method, status, client, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (endpoint, method, status, client, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


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
