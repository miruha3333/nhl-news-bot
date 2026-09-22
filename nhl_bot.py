import os
import re
import random
import hashlib
import html
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

try:
    from PIL import Image
except ImportError:
    Image = None


SOURCE_URL = "https://heavy.com/sports/nhl/"
TELEGRAM_TOKEN = os.getenv("TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

DATABASE_FILE = "nhl_bot.db"

MAX_NEWS = 30

GEMINI_PRIMARY_MODEL = "gemini-3.5-flash"
GEMINI_FALLBACK_MODEL = "gemini-3.5-flash-lite"

GEMINI_TIMEOUT = 60
GEMINI_RETRY_ATTEMPTS = 3
GEMINI_RETRY_BASE_DELAY = 5
GEMINI_RETRY_MAX_DELAY = 20
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free").strip()
OPENROUTER_TIMEOUT = 60
OPENROUTER_RETRY_ATTEMPTS = 2
OPENROUTER_RETRY_BASE_DELAY = 5
OPENROUTER_RETRY_MAX_DELAY = 15
TELEGRAM_TIMEOUT = 60

SOURCE_IMAGE_DOWNLOAD_TIMEOUT = 15
MIN_IMAGE_BYTES = 5000

GEMINI_DELAY = 1.0
MAX_ARTICLE_TEXT = 12000

MAX_POST_CHARS = 850
MAX_POST_PARAGRAPHS = 4

FRESH_DAYS = 90
HISTORICAL_YEAR_TOLERANCE = 3

DATABASE_READY = False
GEMINI_PRIMARY_DISABLED = False


class PostRejected(Exception):
    """The generated post must be skipped and permanently marked as processed."""


class PostValidationServiceError(Exception):
    """The validation service failed; the news should remain retryable."""


# =========================================================
# URL
# =========================================================

def normalize_url(url):
    url = (url or "").strip()

    if not url:
        return ""

    try:
        parsed = urlparse(url)

        host = parsed.netloc.lower().replace("www.", "")
        path = parsed.path.rstrip("/") or "/"

        return urlunparse(
            (
                parsed.scheme.lower(),
                host,
                path,
                "",
                parsed.query,
                "",
            )
        )

    except Exception:
        return url


# =========================================================
# DATABASE HELPERS
# =========================================================

def table_exists(conn, table):
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
        AND name = ?
        """,
        (table,),
    ).fetchone()

    return row is not None


def get_existing_columns(conn, table):
    if not table_exists(conn, table):
        return set()

    rows = conn.execute(
        f'PRAGMA table_info("{table}")'
    ).fetchall()

    return {row[1] for row in rows}


def add_column_if_missing(
    conn,
    table,
    column,
    definition,
):
    columns = get_existing_columns(conn, table)

    if column not in columns:
        print(
            f"[DATABASE] Adding missing column "
            f"{table}.{column}"
        )

        conn.execute(
            f'ALTER TABLE "{table}" '
            f'ADD COLUMN "{column}" {definition}'
        )


# =========================================================
# DATABASE MIGRATION
# =========================================================

def remove_duplicate_news(conn):
    if not table_exists(conn, "news"):
        return

    rows = conn.execute(
        """
        SELECT rowid, url
        FROM news
        ORDER BY rowid
        """
    ).fetchall()

    seen = set()

    for rowid, url in rows:
        normalized = normalize_url(url)

        if not normalized:
            continue

        if normalized in seen:
            conn.execute(
                "DELETE FROM news WHERE rowid = ?",
                (rowid,),
            )
        else:
            seen.add(normalized)


def remove_duplicate_images(conn):
    if not table_exists(conn, "images"):
        return

    rows = conn.execute(
        """
        SELECT rowid, url
        FROM images
        ORDER BY rowid
        """
    ).fetchall()

    seen = set()

    for rowid, url in rows:
        normalized = normalize_url(url)

        if not normalized:
            continue

        if normalized in seen:
            conn.execute(
                "DELETE FROM images WHERE rowid = ?",
                (rowid,),
            )
        else:
            seen.add(normalized)


def normalize_existing_urls(conn, table):
    if not table_exists(conn, table):
        return

    columns = get_existing_columns(conn, table)

    if "url" not in columns:
        return

    rows = conn.execute(
        f'SELECT rowid, url FROM "{table}"'
    ).fetchall()

    for rowid, url in rows:
        normalized = normalize_url(url)

        if normalized and normalized != url:
            conn.execute(
                f'UPDATE "{table}" SET url = ? WHERE rowid = ?',
                (normalized, rowid),
            )


def migrate_database(conn):
    print("[DATABASE] Checking database schema...")

    # -----------------------------------------------------
    # NEWS
    # -----------------------------------------------------

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            title TEXT,
            source TEXT,
            published TEXT,
            processed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    add_column_if_missing(
        conn,
        "news",
        "processed",
        "INTEGER NOT NULL DEFAULT 1",
    )

    add_column_if_missing(
        conn,
        "news",
        "title",
        "TEXT",
    )

    add_column_if_missing(
        conn,
        "news",
        "source",
        "TEXT",
    )

    add_column_if_missing(
        conn,
        "news",
        "published",
        "TEXT",
    )

    add_column_if_missing(
        conn,
        "news",
        "created_at",
        "TEXT",
    )

    # -----------------------------------------------------
    # IMAGES
    # -----------------------------------------------------

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    add_column_if_missing(
        conn,
        "images",
        "used",
        "INTEGER NOT NULL DEFAULT 0",
    )

    add_column_if_missing(
        conn,
        "images",
        "created_at",
        "TEXT",
    )

    # -----------------------------------------------------
    # ERRORS
    # -----------------------------------------------------

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT,
            error TEXT,
            error_message TEXT,
            stage TEXT NOT NULL DEFAULT 'unknown',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    add_column_if_missing(
        conn,
        "errors",
        "url",
        "TEXT",
    )

    add_column_if_missing(
        conn,
        "errors",
        "error",
        "TEXT",
    )

    add_column_if_missing(
        conn,
        "errors",
        "error_message",
        "TEXT NOT NULL DEFAULT ''",
    )

    add_column_if_missing(
        conn,
        "errors",
        "stage",
        "TEXT NOT NULL DEFAULT 'unknown'",
    )

    add_column_if_missing(
        conn,
        "errors",
        "created_at",
        "TEXT",
    )

    # -----------------------------------------------------
    # NORMALIZE + DUPLICATES
    # -----------------------------------------------------

    print("[DATABASE] Normalizing existing URLs...")

    normalize_existing_urls(
        conn,
        "news",
    )

    normalize_existing_urls(
        conn,
        "images",
    )

    print("[DATABASE] Checking duplicate news URLs...")

    remove_duplicate_news(conn)

    print("[DATABASE] Checking duplicate image URLs...")

    remove_duplicate_images(conn)

    # -----------------------------------------------------
    # UNIQUE INDEXES
    # -----------------------------------------------------

    print("[DATABASE] Creating unique index for news.url...")

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
        idx_news_url_unique
        ON news(url)
        """
    )

    print("[DATABASE] Creating unique index for images.url...")

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
        idx_images_url_unique
        ON images(url)
        """
    )

    conn.commit()

    print("[DATABASE] Schema check complete.")


def get_db():
    global DATABASE_READY

    conn = sqlite3.connect(
        DATABASE_FILE,
        timeout=30,
    )

    conn.execute(
        "PRAGMA journal_mode=WAL"
    )

    conn.execute(
        "PRAGMA busy_timeout=30000"
    )

    # ВАЖНО:
    # миграция выполняется только один раз
    # за весь запуск программы.
    if not DATABASE_READY:
        migrate_database(conn)
        DATABASE_READY = True

    return conn


# =========================================================
# NEWS DATABASE
# =========================================================

def save_news(item):
    conn = get_db()

    url = normalize_url(
        item.get("url", "")
    )

    row = conn.execute(
        """
        SELECT id
        FROM news
        WHERE url = ?
        """,
        (url,),
    ).fetchone()

    if row:
        news_id = row[0]

        conn.execute(
            """
            UPDATE news
            SET title = ?,
                source = ?,
                published = ?
            WHERE id = ?
            """,
            (
                item.get("title", ""),
                item.get("source", ""),
                item.get("published", ""),
                news_id,
            ),
        )

    else:
        created_at = datetime.now(timezone.utc).isoformat()

        cursor = conn.execute(
            """
            INSERT INTO news (
                url,
                title,
                source,
                published,
                processed,
                created_at
            )
            VALUES (?, ?, ?, ?, 0, ?)
            """,
            (
                url,
                item.get("title", ""),
                item.get("source", ""),
                item.get("published", ""),
                created_at,
            ),
        )

        news_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return news_id


def is_processed(url):
    conn = get_db()

    row = conn.execute(
        """
        SELECT processed
        FROM news
        WHERE url = ?
        """,
        (normalize_url(url),),
    ).fetchone()

    conn.close()

    return bool(
        row and row[0]
    )


def mark_processed(url):
    conn = get_db()

    conn.execute(
        """
        UPDATE news
        SET processed = 1
        WHERE url = ?
        """,
        (normalize_url(url),),
    )

    conn.commit()
    conn.close()


def get_retryable_gemini_items():
    """Return previously discovered news that failed only because Gemini was unavailable.

    These rows intentionally remain processed=0. That means a temporary Gemini
    outage cannot permanently lose a news item just because it fell out of the
    current top-30 Heavy listing before the next successful run.
    """
    conn = get_db()

    rows = conn.execute(
        """
        SELECT n.url, n.title, n.source, n.published
        FROM news AS n
        WHERE n.processed = 0
          AND EXISTS (
              SELECT 1
              FROM errors AS e
              WHERE e.url = n.url
                AND e.id = (
                    SELECT MAX(e2.id)
                    FROM errors AS e2
                    WHERE e2.url = n.url
                )
                AND e.stage = 'gemini_service'
          )
        ORDER BY n.id ASC
        """
    ).fetchall()

    conn.close()

    return [
        {
            "url": row[0],
            "title": row[1] or "",
            "source": row[2] or "heavy.com",
            "published": row[3] or "",
            "summary": "",
        }
        for row in rows
    ]


# =========================================================
# IMAGE DATABASE
# =========================================================

def save_image(url, used=0):
    url = normalize_url(url)

    if not url:
        return

    conn = get_db()

    row = conn.execute(
        """
        SELECT id
        FROM images
        WHERE url = ?
        """,
        (url,),
    ).fetchone()

    if row:
        conn.execute(
            """
            UPDATE images
            SET used = ?
            WHERE id = ?
            """,
            (
                used,
                row[0],
            ),
        )

    else:
        created_at = datetime.now(timezone.utc).isoformat()

        conn.execute(
            """
            INSERT INTO images (
                url,
                used,
                created_at
            )
            VALUES (?, ?, ?)
            """,
            (
                url,
                used,
                created_at,
            ),
        )

    conn.commit()
    conn.close()


# =========================================================
# ERROR LOGGING
# =========================================================

def log_error(
    url,
    error,
    stage="unknown",
):
    message = str(error)[:4000]

    try:
        conn = get_db()

        columns = get_existing_columns(
            conn,
            "errors",
        )

        # error_message existed in an older database schema and may be NOT NULL.
        # Therefore, when the column exists, we ALWAYS write it.
        fields = []
        values = []

        if "url" in columns:
            fields.append("url")
            values.append(normalize_url(url))

        if "error" in columns:
            fields.append("error")
            values.append(message)

        if "error_message" in columns:
            fields.append("error_message")
            values.append(message)

        if "stage" in columns:
            fields.append("stage")
            values.append(stage or "unknown")

        if "created_at" in columns:
            fields.append("created_at")
            values.append(
                datetime.now(
                    timezone.utc
                ).isoformat()
            )

        if not fields:
            raise RuntimeError(
                "Errors table has no writable columns"
            )

        placeholders = ",".join(
            "?" for _ in fields
        )

        conn.execute(
            f"""
            INSERT INTO errors (
                {",".join(fields)}
            )
            VALUES (
                {placeholders}
            )
            """,
            tuple(values),
        )

        conn.commit()
        conn.close()

    except Exception as logging_exc:
        # Logging must never crash the bot after the original error.
        print(
            "[ERROR LOGGER FAILED] "
            f"{logging_exc}"
        )
        print(
            "[ORIGINAL ERROR] "
            f"{stage}: {message}"
        )


# =========================================================
# HEAVY LISTING PAGE
# =========================================================

def is_heavy_article_url(url):
    normalized = normalize_url(url)
    parsed = urlparse(normalized)

    if parsed.netloc != "heavy.com":
        return False

    path = parsed.path.rstrip("/")

    if not path.startswith("/sports/nhl/"):
        return False

    if path in ("/sports/nhl", "/sports/nhl/"):
        return False

    return True


def extract_listing_title(anchor):
    title = (
        anchor.get_text(" ", strip=True)
        or anchor.get("aria-label", "")
        or anchor.get("title", "")
        or ""
    )

    return re.sub(r"\s+", " ", title).strip()


def load_news():
    print(f"[HEAVY] Loading source page: {SOURCE_URL}")

    response = requests.get(
        SOURCE_URL,
        headers={
            "User-Agent":
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36 NHLNewsBot/1.0"
        },
        timeout=30,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    result = []
    seen = set()

    for anchor in soup.find_all("a", href=True):
        url = normalize_url(urljoin(response.url, anchor.get("href", "")))

        if not is_heavy_article_url(url) or url in seen:
            continue

        title = extract_listing_title(anchor)
        if not title:
            continue

        seen.add(url)
        result.append({
            "url": url,
            "title": title,
            "source": "heavy.com",
            "published": "",
            "summary": "",
        })

        if len(result) >= MAX_NEWS:
            break

    print(f"[HEAVY] Articles found: {len(result)}")

    for index, item in enumerate(result, 1):
        print(f"[HEAVY] {index}. {item['title']} | {item['url']}")

    return result


# ARTICLE
# =========================================================

def normalize_image_url(image_url, page_url):
    image_url = (image_url or "").strip()

    if not image_url:
        return ""

    image_url = urljoin(
        page_url,
        image_url,
    )

    parsed = urlparse(image_url)

    if parsed.scheme not in ("http", "https"):
        return ""

    return image_url


def extract_jsonld_images(value, page_url):
    images = []

    if isinstance(value, str):
        normalized = normalize_image_url(
            value,
            page_url,
        )

        if normalized:
            images.append(normalized)

    elif isinstance(value, list):
        for item in value:
            images.extend(
                extract_jsonld_images(
                    item,
                    page_url,
                )
            )

    elif isinstance(value, dict):
        for key in (
            "url",
            "contentUrl",
            "thumbnailUrl",
        ):
            if key in value:
                images.extend(
                    extract_jsonld_images(
                        value[key],
                        page_url,
                    )
                )

        for key in (
            "image",
            "images",
            "thumbnail",
        ):
            if key in value:
                images.extend(
                    extract_jsonld_images(
                        value[key],
                        page_url,
                    )
                )

        for item in value.get("@graph", []):
            images.extend(
                extract_jsonld_images(
                    item,
                    page_url,
                )
            )

    return images


def extract_source_images(soup, page_url):
    candidates = []

    for meta in soup.find_all(
        "meta"
    ):
        prop = (
            meta.get("property", "")
            or meta.get("name", "")
        ).lower().strip()

        if prop in (
            "og:image",
            "og:image:url",
            "og:image:secure_url",
        ):
            image_url = normalize_image_url(
                meta.get("content", ""),
                page_url,
            )

            if image_url:
                candidates.append(
                    (image_url, "og:image")
                )

        elif prop in (
            "twitter:image",
            "twitter:image:src",
        ):
            image_url = normalize_image_url(
                meta.get("content", ""),
                page_url,
            )

            if image_url:
                candidates.append(
                    (image_url, "twitter:image")
                )

    for link in soup.find_all(
        "link"
    ):
        rel = [
            str(value).lower()
            for value in link.get("rel", [])
        ]

        if "image_src" in rel:
            image_url = normalize_image_url(
                link.get("href", ""),
                page_url,
            )

            if image_url:
                candidates.append(
                    (image_url, "link:image_src")
                )

    for script in soup.find_all(
        "script",
        attrs={"type": "application/ld+json"},
    ):
        raw = script.string or script.get_text()

        if not raw.strip():
            continue

        try:
            import json

            data = json.loads(raw)
        except Exception:
            continue

        for image_url in extract_jsonld_images(
            data,
            page_url,
        ):
            candidates.append(
                (image_url, "json-ld")
            )

    unique = []
    seen = set()

    for image_url, source in candidates:
        if image_url in seen:
            continue

        seen.add(image_url)
        unique.append(
            (image_url, source)
        )

    return unique


def fetch_article(
    url,
    fallback_summary="",
):
    headers = {
        "User-Agent":
            "Mozilla/5.0 "
            "(NHLNewsBot/1.0)"
    }

    response = requests.get(
        url,
        headers=headers,
        timeout=30,
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    source_images = extract_source_images(
        soup,
        response.url or url,
    )

    if source_images:
        print(
            "[IMAGE] Source page image candidates: "
            f"{len(source_images)}"
        )

    text_soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    for tag in text_soup(
        [
            "script",
            "style",
            "noscript",
            "svg",
        ]
    ):
        tag.decompose()

    text = text_soup.get_text(
        " ",
        strip=True,
    )

    if len(text) < 300:
        text = fallback_summary

    text = re.sub(
        r"\s+",
        " ",
        text,
    ).strip()

    return (
        text[:MAX_ARTICLE_TEXT],
        source_images,
    )


def download_source_image(
    image_url,
    article_url,
    image_source="unknown",
):
    """Download an image directly from the Heavy article page candidate."""
    if not image_url:
        return None

    headers = {
        "User-Agent":
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0 Safari/537.36 NHLNewsBot/1.0",
        "Referer": article_url,
        "Accept":
            "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }

    print(
        "[IMAGE] Trying source image: "
        f"{image_source} | {image_url}"
    )

    try:
        response = requests.get(
            image_url,
            headers=headers,
            timeout=SOURCE_IMAGE_DOWNLOAD_TIMEOUT,
            allow_redirects=True,
        )
    except requests.exceptions.RequestException as exc:
        print(
            "[IMAGE] Download failed: "
            f"{exc}"
        )
        return None

    if response.status_code >= 400:
        print(
            "[IMAGE] Download failed: HTTP "
            f"{response.status_code}"
        )
        return None

    content = response.content

    if len(content) < MIN_IMAGE_BYTES:
        print(
            "[IMAGE] Downloaded file is too small: "
            f"{len(content)} bytes"
        )
        return None

    content_type = (
        response.headers.get("Content-Type", "")
        .split(";", 1)[0]
        .strip()
        .lower()
    )

    # Some CDN responses omit Content-Type, so inspect common image signatures too.
    if not content_type.startswith("image/"):
        if content.startswith(b"\xff\xd8\xff"):
            content_type = "image/jpeg"
        elif content.startswith(b"\x89PNG\r\n\x1a\n"):
            content_type = "image/png"
        elif content.startswith((b"GIF87a", b"GIF89a")):
            content_type = "image/gif"
        elif content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            content_type = "image/webp"
        else:
            print(
                "[IMAGE] Response is not an image: "
                f"{content_type or 'unknown content type'}"
            )
            return None

    extension_map = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/avif": ".avif",
    }

    extension = extension_map.get(content_type, ".img")

    try:
        temp_file = tempfile.NamedTemporaryFile(
            prefix="nhl_source_image_",
            suffix=extension,
            delete=False,
        )
        temp_file.write(content)
        temp_file.flush()
        temp_file.close()
    except OSError as exc:
        print(
            "[IMAGE] Could not save downloaded image: "
            f"{exc}"
        )
        return None

    image_path = temp_file.name

    print(
        "[IMAGE] Downloaded successfully: "
        f"{len(content)} bytes | {content_type} | {image_path}"
    )

    save_image(
        response.url or image_url,
        used=1,
    )

    return image_path


# =========================================================
# GEMINI
# =========================================================

def gemini_request(
    model,
    prompt,
):
    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{model}:generateContent"
    )

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": prompt
                    }
                ]
            }
        ],
        "generationConfig": {
            "maxOutputTokens": 1000,
            "thinkingConfig": {
                "thinkingLevel": "minimal"
            },
        },
    }

    try:
        response = requests.post(
            url,
            params={"key": GEMINI_API_KEY},
            json=payload,
            timeout=(10, GEMINI_TIMEOUT),
        )
    except requests.exceptions.ReadTimeout as exc:
        raise RuntimeError(
            f"Gemini read timeout after {GEMINI_TIMEOUT}s"
        ) from exc
    except requests.exceptions.ConnectTimeout as exc:
        raise RuntimeError(
            "Gemini connection timeout"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(
            f"Gemini network error: {exc}"
        ) from exc

    if response.status_code >= 400:
        raise RuntimeError(
            "Gemini HTTP "
            f"{response.status_code}: "
            f"{response.text[:1000]}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            "Gemini returned invalid JSON"
        ) from exc

    candidates = data.get("candidates", [])

    if not candidates:
        raise RuntimeError("Gemini returned no candidates")

    parts = (
        candidates[0]
        .get("content", {})
        .get("parts", [])
    )

    text = "".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, dict)
    ).strip()

    if not text:
        finish_reason = candidates[0].get(
            "finishReason",
            "unknown",
        )
        raise RuntimeError(
            "Gemini returned an empty response; "
            f"finish reason: {finish_reason}"
        )

    return text


def gemini_request_with_retry(model, prompt, label):
    """Retry transient Gemini failures before giving up on a model.

    Google recommends exponential backoff for transient 429/5xx errors.
    Read/connect timeouts are also treated as transient because they can occur
    when the model is overloaded and the request never completes.
    """
    last_error = None

    for attempt in range(1, GEMINI_RETRY_ATTEMPTS + 1):
        try:
            if attempt > 1:
                print(
                    f"[GEMINI {label}] Retry attempt "
                    f"{attempt}/{GEMINI_RETRY_ATTEMPTS}"
                )

            return gemini_request(model, prompt)

        except Exception as exc:
            last_error = exc

            if (
                not is_gemini_temporary_error(exc)
                or attempt >= GEMINI_RETRY_ATTEMPTS
            ):
                raise

            delay = min(
                GEMINI_RETRY_MAX_DELAY,
                GEMINI_RETRY_BASE_DELAY * (2 ** (attempt - 1)),
            )
            delay += random.uniform(0, 2)

            print(
                f"[GEMINI {label}] Temporary error; "
                f"retrying in {delay:.1f}s: {exc}"
            )
            time.sleep(delay)

    raise last_error



def openrouter_request(
    model,
    prompt,
):
    """Generate a post through OpenRouter as the emergency LLM fallback."""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not configured")

    url = "https://openrouter.ai/api/v1/chat/completions"

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        "max_tokens": 1000,
    }

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "X-Title": "NHL News Bot",
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=(10, OPENROUTER_TIMEOUT),
        )
    except requests.exceptions.ReadTimeout as exc:
        raise RuntimeError(
            f"OpenRouter read timeout after {OPENROUTER_TIMEOUT}s"
        ) from exc
    except requests.exceptions.ConnectTimeout as exc:
        raise RuntimeError(
            "OpenRouter connection timeout"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(
            f"OpenRouter network error: {exc}"
        ) from exc

    if response.status_code >= 400:
        raise RuntimeError(
            "OpenRouter HTTP "
            f"{response.status_code}: "
            f"{response.text[:1000]}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            "OpenRouter returned invalid JSON"
        ) from exc

    choices = data.get("choices", [])

    if not choices:
        raise RuntimeError("OpenRouter returned no choices")

    message = choices[0].get("message", {})
    text = message.get("content", "")

    if isinstance(text, list):
        text = "".join(
            item.get("text", "")
            for item in text
            if isinstance(item, dict)
        )

    text = str(text or "").strip()

    if not text:
        finish_reason = choices[0].get(
            "finish_reason",
            "unknown",
        )
        raise RuntimeError(
            "OpenRouter returned an empty response; "
            f"finish reason: {finish_reason}"
        )

    return text


def is_openrouter_temporary_error(error):
    error_text = str(error).lower()

    return any(
        marker in error_text
        for marker in (
            "http 408",
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "rate limit",
            "too many requests",
            "timeout",
            "timed out",
            "network error",
            "connection timeout",
            "temporarily unavailable",
            "overloaded",
            "high demand",
        )
    )


def openrouter_request_with_retry(model, prompt):
    """Retry transient OpenRouter failures before giving up."""
    last_error = None

    for attempt in range(1, OPENROUTER_RETRY_ATTEMPTS + 1):
        try:
            if attempt > 1:
                print(
                    f"[OPENROUTER] Retry attempt "
                    f"{attempt}/{OPENROUTER_RETRY_ATTEMPTS}"
                )

            return openrouter_request(model, prompt)

        except Exception as exc:
            last_error = exc

            if (
                not is_openrouter_temporary_error(exc)
                or attempt >= OPENROUTER_RETRY_ATTEMPTS
            ):
                raise

            delay = min(
                OPENROUTER_RETRY_MAX_DELAY,
                OPENROUTER_RETRY_BASE_DELAY * (2 ** (attempt - 1)),
            )
            delay += random.uniform(0, 2)

            print(
                "[OPENROUTER] Temporary error; "
                f"retrying in {delay:.1f}s: {exc}"
            )
            time.sleep(delay)

    raise last_error

def clean_post(text):
    text = (text or "").strip()
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    text = re.sub(
        r"^```(?:text)?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"\s*```$",
        "",
        text,
    )

    text = re.sub(
        r"^(Пост|Текст)\s*:\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )

    return text.strip()


def split_sentences(text):
    return [
        part.strip()
        for part in re.split(r"(?<=[.!?…])\s+", text.strip())
        if part.strip()
    ]


def build_short_paragraphs(text):
    """Make the generated text compact and readable without another Gemini call."""
    text = clean_post(text)

    raw_paragraphs = [
        re.sub(r"[ \t]+", " ", paragraph).strip()
        for paragraph in re.split(r"\n\s*\n+", text)
        if paragraph.strip()
    ]

    if not raw_paragraphs:
        return ""

    paragraphs = []

    # If Gemini returned one large block, create paragraphs from sentences.
    if len(raw_paragraphs) == 1:
        sentences = split_sentences(raw_paragraphs[0])

        if len(sentences) > 1:
            current = []

            for sentence in sentences:
                current.append(sentence)

                # Prefer short paragraphs of one or two sentences.
                if len(current) >= 2:
                    paragraphs.append(" ".join(current))
                    current = []

            if current:
                paragraphs.append(" ".join(current))
        else:
            paragraphs = raw_paragraphs[:]
    else:
        paragraphs = raw_paragraphs[:]

    # Never create a wall of tiny paragraphs. Merge overflow into the last
    # allowed paragraph rather than silently dropping information.
    if len(paragraphs) > MAX_POST_PARAGRAPHS:
        paragraphs = (
            paragraphs[:MAX_POST_PARAGRAPHS - 1]
            + [" ".join(paragraphs[MAX_POST_PARAGRAPHS - 1:])]
        )

    # Keep the whole post comfortably below Telegram's caption limit.
    shortened = []
    total = 0

    for paragraph in paragraphs:
        separator = 2 if shortened else 0
        available = MAX_POST_CHARS - total - separator

        if available <= 0:
            break

        if len(paragraph) <= available:
            shortened.append(paragraph)
            total += separator + len(paragraph)
            continue

        sentences = split_sentences(paragraph)
        added = []

        for sentence in sentences:
            sentence_separator = 1 if added else 0
            if len(" ".join(added)) + sentence_separator + len(sentence) <= available:
                added.append(sentence)
            else:
                break

        if added:
            shortened.append(" ".join(added))
            total += separator + len(shortened[-1])
        else:
            # If one sentence is unusually long, keep as much of it as
            # possible and let the final word-boundary trim handle it.
            fallback = paragraph[:available].rsplit(" ", 1)[0].rstrip(" ,;:-")
            if fallback:
                shortened.append(fallback + "…")

        break

    result = "\n\n".join(shortened).strip()

    # A single unusually long sentence is still preferable to a Telegram
    # API failure, so trim only at a word boundary as a last resort.
    if len(result) > MAX_POST_CHARS:
        result = result[:MAX_POST_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"

    return result


def format_telegram_post(text):
    """Apply restrained Telegram HTML formatting to the final post."""
    paragraphs = [
        paragraph.strip()
        for paragraph in text.split("\n\n")
        if paragraph.strip()
    ]

    if not paragraphs:
        return ""

    escaped = [html.escape(paragraph, quote=False) for paragraph in paragraphs]

    # The lead paragraph is bold; the rest stays clean and readable.
    escaped[0] = f"<b>{escaped[0]}</b>"

    return "\n\n".join(escaped)

def contains_technical_content(text):
    lowered = text.lower()

    technical_patterns = (
        r"```",
        r"\{\s*[\"']",
        r"[\"']?(api|json|http|https|endpoint|request|response|prompt|system message|system prompt)\b",
        r"stack\s*trace",
        r"traceback",
        r"generationconfig",
        r"quota\s*(exceeded|failure)",
        r"resource_exhausted",
        r"rate\s*limit",
        r"internal server error",
        r"validation service",
        r"gemini\s+http\s+\d{3}",
        r"error\s*code\s*[:=]\s*\d{3}",
    )

    return any(
        re.search(pattern, lowered)
        for pattern in technical_patterns
    )


def validate_post_content(text):
    """Reject only clear garbage, non-Russian or technical output."""
    text = build_short_paragraphs(text)

    if not text:
        raise PostRejected("Generated post is empty")

    if "\ufffd" in text:
        raise PostRejected("Generated post contains a replacement character")

    if re.search(r"(.)\1{7,}", text):
        raise PostRejected("Generated post contains repeated garbage characters")

    if contains_technical_content(text):
        raise PostRejected("Generated post contains technical information")

    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", text)
    cyrillic = re.findall(r"[А-Яа-яЁё]", text)

    if not letters:
        raise PostRejected("Generated post contains no normal words")

    russian_ratio = len(cyrillic) / len(letters)

    if russian_ratio < 0.45:
        raise PostRejected("Generated post is not sufficiently Russian")

    if any(
        ord(char) < 32 and char not in "\n\r\t"
        for char in text
    ):
        raise PostRejected("Generated post contains control characters")

    return text

def generate_post_prompt(title, article):
    return f"""
Ты пишешь короткий пост для русскоязычного Telegram-канала о хоккейной новости.

Твоя задача — дать читателю быструю и понятную выжимку самого важного. Не пересказывай статью целиком.

Правила:
- пиши только на русском языке;
- передавай только информацию из исходного материала;
- не добавляй факты от себя;
- пост должен относиться именно к этой новости;
- убирай второстепенные детали, фон и повторы, если без них понятен смысл;
- ориентируйся примерно на 450–750 символов; абсолютный максимум — около 850 символов;
- обычно достаточно 2–4 коротких абзацев;
- каждый абзац должен быть небольшим, не превращай пост в сплошную простыню;
- первый абзац должен сразу сообщать главное событие;
- следующие абзацы могут дать ключевые детали и контекст;
- если новость можно нормально объяснить в 2–3 предложениях, не растягивай её;
- не повторяй одну и ту же мысль разными словами;
- не используй списки и подзаголовки;
- названия команд пиши без кавычек;
- не добавляй эмодзи;
- не используй HTML, Markdown или другие специальные обозначения форматирования;
- не используй служебные пометки, код, JSON, API-ответы, промпты или другую техническую информацию;
- если исходный материал невозможно нормально превратить в русскую новостную выжимку, верни ровно REJECT;
- если материал нормальный, верни только готовый текст поста с обычными переносами строк между абзацами.

Заголовок статьи:
{title}

Материал статьи:
{article}
""".strip()

def is_gemini_temporary_error(error):
    error_text = str(error).lower()

    return any(
        marker in error_text
        for marker in (
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "quota",
            "high demand",
            "unavailable",
            "resource_exhausted",
            "rate limit",
            "timeout",
            "timed out",
            "network error",
            "connection timeout",
            "404",
            "not found",
        )
    )


def generate_post(title, article):
    global GEMINI_PRIMARY_DISABLED

    if not GEMINI_API_KEY and not OPENROUTER_API_KEY:
        raise RuntimeError(
            "Neither GEMINI_API_KEY nor OPENROUTER_API_KEY is configured"
        )

    prompt = generate_post_prompt(title, article)

    last_error = None

    # 1) Direct Gemini primary.
    # 2) Direct Gemini fallback.
    # 3) OpenRouter emergency fallback.
    if GEMINI_API_KEY:
        models = []

        if not GEMINI_PRIMARY_DISABLED:
            models.append((GEMINI_PRIMARY_MODEL, "PRIMARY"))

        models.append((GEMINI_FALLBACK_MODEL, "FALLBACK"))

        for model, label in models:
            print(f"[GEMINI {label}] Using {model}")

            try:
                result = gemini_request_with_retry(
                    model,
                    prompt,
                    label,
                )
                result = clean_post(result)

                if result.upper() == "REJECT":
                    last_error = RuntimeError(
                        f"Gemini {label.lower()} rejected the article as not relevant"
                    )
                    print(
                        f"[GEMINI {label} REJECTED] "
                        "Model returned REJECT; trying the next LLM fallback."
                    )
                    continue

                validated = validate_post_content(result)
                print(
                    f"[POST] Generated successfully by Gemini {label.lower()}: {model}"
                )
                return validated

            except PostRejected:
                raise

            except Exception as exc:
                last_error = exc

                print(
                    f"[GEMINI {label} ERROR] {exc}"
                )

                if label == "PRIMARY" and is_gemini_temporary_error(exc):
                    GEMINI_PRIMARY_DISABLED = True
                    print(
                        "[GEMINI] Primary disabled for the remainder of this run; "
                        "using fallback model."
                    )
                    continue

                if label == "PRIMARY":
                    continue

                break

    # OpenRouter is intentionally the emergency provider rather than the normal
    # path. It keeps the bot alive when Google's direct API is overloaded or down.
    if OPENROUTER_API_KEY:
        print(f"[OPENROUTER] Using {OPENROUTER_MODEL}")

        try:
            result = openrouter_request_with_retry(
                OPENROUTER_MODEL,
                prompt,
            )
            result = clean_post(result)

            if result.upper() == "REJECT":
                last_error = RuntimeError(
                    "OpenRouter rejected the article as not relevant"
                )
                print(
                    "[OPENROUTER REJECTED] Model returned REJECT; "
                    "content generation failed for this article."
                )
                raise PostValidationServiceError(
                    "All configured LLMs rejected or failed to generate a post"
                )

            validated = validate_post_content(result)
            print(
                f"[POST] Generated successfully by OpenRouter: {OPENROUTER_MODEL}"
            )
            return validated

        except PostRejected:
            raise

        except Exception as exc:
            last_error = exc
            print(f"[OPENROUTER ERROR] {exc}")

    raise PostValidationServiceError(
        f"LLM content generation service failed: {last_error}"
    )


# =========================================================
# TELEGRAM
# =========================================================

def prepare_telegram_photo(image_path):
    """Convert the source image to a Telegram-friendly JPEG photo.

    Telegram may reject otherwise valid WebP/AVIF images or unusual dimensions
    when they are uploaded through sendPhoto. Re-encode them as JPEG and keep
    the dimensions within a conservative range.
    """
    if not image_path:
        return None

    if Image is None:
        raise RuntimeError(
            "Pillow is required to convert images for Telegram; "
            "add Pillow to requirements.txt"
        )

    try:
        with Image.open(image_path) as source:
            source.load()

            original_size = source.size
            image = source.convert("RGB")

            max_dimension = 4096
            width, height = image.size

            if width <= 0 or height <= 0:
                raise RuntimeError("Downloaded image has invalid dimensions")

            scale = min(
                1.0,
                max_dimension / float(width),
                max_dimension / float(height),
            )

            new_size = (width, height)

            if scale < 1.0:
                new_size = (
                    max(1, int(round(width * scale))),
                    max(1, int(round(height * scale))),
                )
                image = image.resize(
                    new_size,
                    Image.Resampling.LANCZOS,
                )

            output = tempfile.NamedTemporaryFile(
                prefix="nhl_telegram_photo_",
                suffix=".jpg",
                delete=False,
            )
            output_path = output.name
            output.close()

            image.save(
                output_path,
                format="JPEG",
                quality=92,
                optimize=True,
            )
            image.close()

            print(
                "[IMAGE] Prepared Telegram photo: "
                f"{original_size[0]}x{original_size[1]} -> "
                f"{new_size[0]}x{new_size[1]} | "
                f"JPEG | {output_path}"
            )

            return output_path

    except Exception as exc:
        print(
            "[IMAGE] Telegram JPEG conversion failed: "
            f"{exc}"
        )
        raise


def send_telegram(
    post,
    image_path=None,
):
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TOKEN is not configured")

    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("CHAT_ID is not configured")

    base_url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}"
    )

    formatted_post = format_telegram_post(post)

    if not formatted_post:
        raise RuntimeError("Formatted Telegram post is empty")

    prepared_image_path = None

    if image_path:
        prepared_image_path = prepare_telegram_photo(image_path)

        with open(prepared_image_path, "rb") as image_file:
            response = requests.post(
                f"{base_url}/sendPhoto",
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "caption": formatted_post,
                    "parse_mode": "HTML",
                },
                files={"photo": ("nhl.jpg", image_file, "image/jpeg")},
                timeout=TELEGRAM_TIMEOUT,
            )
    else:
        response = requests.post(
            f"{base_url}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": formatted_post,
                "parse_mode": "HTML",
            },
            timeout=TELEGRAM_TIMEOUT,
        )

    if response.status_code >= 400:
        raise RuntimeError(
            "Telegram HTTP "
            f"{response.status_code}: "
            f"{response.text[:1000]}"
        )

    if prepared_image_path and prepared_image_path != image_path:
        try:
            os.unlink(prepared_image_path)
        except OSError:
            pass

def process_news(
    item,
    index,
    total,
):
    print(f"[BOT] Processing {index}/{total}")

    url = item["url"]

    try:
        save_news(item)

        article, source_images = fetch_article(
            url,
            item.get("summary", ""),
        )

        post = generate_post(
            item["title"],
            article,
        )

        image = None

        for source_image_url, image_source in source_images:
            image = download_source_image(
                source_image_url,
                url,
                image_source,
            )

            if image:
                break

        if image is None:
            raise RuntimeError(
                "Could not download any image from the article page"
            )

        send_telegram(
            post,
            image,
        )

        mark_processed(url)

        print("[BOT] Published successfully")
        return True

    except PostRejected as exc:
        print(f"[POST SKIPPED] {exc}")

        log_error(
            url,
            str(exc),
            "post_rejected",
        )

        # A real content rejection is permanent: do not retry it forever.
        mark_processed(url)
        return False

    except PostValidationServiceError as exc:
        print(f"[POST VALIDATION ERROR] {exc}")

        log_error(
            url,
            str(exc),
            "gemini_service",
        )

        # Service failures remain unprocessed and will be retried later.
        return False

    except Exception as exc:
        message = str(exc)

        stage = "processing"

        if "CHAT_ID" in message or "Telegram" in message:
            stage = "telegram"
            print(f"[TELEGRAM ERROR] {message}")

        elif "Gemini" in message or "GEMINI" in message:
            stage = "gemini"

        elif "image" in message.lower():
            stage = "image"

        elif "article" in message.lower() or "HTTP" in message:
            stage = "article"

        print(f"[FATAL ITEM ERROR] {message}")

        log_error(
            url,
            message,
            stage,
        )

        # Unexpected technical failures also remain retryable.
        return False


# =========================================================
# MAIN
# =========================================================

def main():
    print("=" * 70)
    print("NHL NEWS BOT START")
    print("=" * 70)

    print(f"[CONFIG] Heavy articles limit: {MAX_NEWS}")
    print(f"[CONFIG] Gemini primary: {GEMINI_PRIMARY_MODEL}")
    print(f"[CONFIG] Gemini fallback: {GEMINI_FALLBACK_MODEL}")
    print("[CONFIG] Images: article page only; no image search fallback")
    print("[CONFIG] Posts: concise, 2-4 paragraphs, max 850 chars, HTML formatting")
    print(f"[CONFIG] Gemini retries per model: {GEMINI_RETRY_ATTEMPTS}, timeout: {GEMINI_TIMEOUT}s")
    if OPENROUTER_API_KEY:
        print(
            f"[CONFIG] OpenRouter: {OPENROUTER_MODEL}, "
            f"retries: {OPENROUTER_RETRY_ATTEMPTS}, "
            f"timeout: {OPENROUTER_TIMEOUT}s"
        )
    else:
        print(
            "[CONFIG] OpenRouter: DISABLED (OPENROUTER_API_KEY is missing); "
            f"retries: {OPENROUTER_RETRY_ATTEMPTS}, "
            f"timeout: {OPENROUTER_TIMEOUT}s"
        )
    print("=" * 70)

    try:
        conn = get_db()
        conn.close()
    except Exception as exc:
        print(f"[DATABASE ERROR] {exc}")
        log_error("", str(exc), "database")
        return

    try:
        items = load_news()
        print("[HEAVY] Source loaded successfully")
    except Exception as exc:
        print(f"[HEAVY ERROR] {exc}")
        log_error(SOURCE_URL, str(exc), "source")
        return

    new_items = [
        item
        for item in items
        if not is_processed(item["url"])
    ]

    retry_items = get_retryable_gemini_items()

    current_urls = {
        normalize_url(item["url"])
        for item in new_items
    }

    for item in retry_items:
        if normalize_url(item["url"]) not in current_urls:
            new_items.append(item)
            current_urls.add(normalize_url(item["url"]))

    print(f"[HEAVY] New articles: {len(new_items)}")
    if retry_items:
        print(
            "[RETRY] Previously failed Gemini items queued: "
            f"{len(retry_items)}"
        )

    if not new_items:
        print("[BOT] No new articles. Nothing to publish.")
        print("[BOT] Run completed successfully with no new articles.")
        print("=" * 70)
        print("NHL NEWS BOT FINISHED")
        print("Published: 0")
        print("Failed: 0")
        print("=" * 70)
        return

    published = 0
    failed = 0

    for index, item in enumerate(new_items, 1):
        if process_news(
            item,
            index,
            len(new_items),
        ):
            published += 1
        else:
            failed += 1

    print("=" * 70)
    print("NHL NEWS BOT FINISHED")
    print(f"Published: {published}")
    print(f"Failed: {failed}")
    print("=" * 70)


if __name__ == "__main__":
    main()
