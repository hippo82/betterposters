"""SQLite persistence."""

import sqlite3
from datetime import datetime, timezone

from . import config

API_STATS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS api_stats (
        endpoint   TEXT,
        method     TEXT,
        status     INTEGER,
        count      INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT,
        PRIMARY KEY (endpoint, method, status)
    )
"""


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
    _migrate_api_stats(conn)
    conn.execute(API_STATS_SCHEMA)
    conn.commit()
    return conn


def _migrate_api_stats(conn):
    """Convert the old per-request api_stats table to the aggregated format."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(api_stats)")]
    if cols and "count" not in cols:
        conn.execute("ALTER TABLE api_stats RENAME TO api_stats_old")
        conn.execute(API_STATS_SCHEMA)
        conn.execute(
            """
            INSERT INTO api_stats (endpoint, method, status, count, updated_at)
            SELECT endpoint, method, status, COUNT(*), MAX(created_at)
            FROM api_stats_old GROUP BY endpoint, method, status
            """
        )
        conn.execute("DROP TABLE api_stats_old")


def log_api_request(endpoint, method, status):
    """Best-effort aggregate count of an API request (never raises)."""
    try:
        conn = sqlite3.connect(config.DB_PATH, timeout=10)
        try:
            conn.execute(API_STATS_SCHEMA)
            conn.execute(
                """
                INSERT INTO api_stats (endpoint, method, status, count, updated_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(endpoint, method, status) DO UPDATE SET
                    count      = count + 1,
                    updated_at = excluded.updated_at
                """,
                (endpoint, method, status, datetime.now(timezone.utc).isoformat()),
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
