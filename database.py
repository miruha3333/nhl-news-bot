import sqlite3
from datetime import datetime, timezone

DB_FILE = "nhl_bot.db"


def get_connection():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def init_db():
    conn = get_connection()

    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT UNIQUE,
                title TEXT,
                url TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL,
                published_at TEXT,
                telegram_message_id TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE,
                created_at TEXT NOT NULL,
                used_at TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT,
                stage TEXT NOT NULL,
                error_message TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        conn.commit()

    finally:
        conn.close()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def news_exists(source_id):
    if not source_id:
        return False

    conn = get_connection()

    try:
        cursor = conn.execute(
            "SELECT 1 FROM news WHERE source_id = ? LIMIT 1",
            (source_id,),
        )
        return cursor.fetchone() is not None

    finally:
        conn.close()


def get_news_status(source_id):
    if not source_id:
        return None

    conn = get_connection()

    try:
        cursor = conn.execute(
            "SELECT status FROM news WHERE source_id = ? LIMIT 1",
            (source_id,),
        )
        row = cursor.fetchone()

        if row:
            return row[0]

        return None

    finally:
        conn.close()


def save_news(source_id, title, url="", status="new"):
    if not source_id:
        return

    conn = get_connection()

    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO news
            (source_id, title, url, status, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                source_id,
                title,
                url or "",
                status,
                utc_now(),
            ),
        )

        conn.commit()

    finally:
        conn.close()


def update_news_status(
    source_id,
    status,
    published_at=None,
    telegram_message_id=None,
):
    if not source_id:
        return

    conn = get_connection()

    try:
        conn.execute(
            """
            UPDATE news
            SET
                status = ?,
                published_at = COALESCE(?, published_at),
                telegram_message_id = COALESCE(?, telegram_message_id)
            WHERE source_id = ?
            """,
            (
                status,
                published_at,
                str(telegram_message_id) if telegram_message_id else None,
                source_id,
            ),
        )

        conn.commit()

    finally:
        conn.close()


def save_error(source_id, stage, error_message):
    conn = get_connection()

    try:
        conn.execute(
            """
            INSERT INTO errors
            (source_id, stage, error_message, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                source_id,
                stage,
                str(error_message),
                utc_now(),
            ),
        )

        conn.commit()

    finally:
        conn.close()


def image_exists(url):
    if not url:
        return False

    conn = get_connection()

    try:
        cursor = conn.execute(
            "SELECT 1 FROM images WHERE url = ? LIMIT 1",
            (url,),
        )
        return cursor.fetchone() is not None

    finally:
        conn.close()


def save_image(url):
    if not url:
        return

    conn = get_connection()

    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO images
            (url, created_at)
            VALUES (?, ?)
            """,
            (
                url,
                utc_now(),
            ),
        )

        conn.commit()

    finally:
        conn.close()


def mark_image_used(url):
    if not url:
        return

    conn = get_connection()

    try:
        conn.execute(
            """
            UPDATE images
            SET used_at = ?
            WHERE url = ?
            """,
            (
                utc_now(),
                url,
            ),
        )

        conn.commit()

    finally:
        conn.close()
