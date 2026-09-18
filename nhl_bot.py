import os
import re
import hashlib
import html
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


SOURCE_URL = "https://heavy.com/sports/nhl/"
TELEGRAM_TOKEN = os.getenv("TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

DATABASE_FILE = "nhl_bot.db"

MAX_NEWS = 30

GEMINI_PRIMARY_MODEL = "gemini-3.5-flash"
GEMINI_FALLBACK_MODEL = "gemini-3.5-flash-lite"

GEMINI_TIMEOUT = 45
TELEGRAM_TIMEOUT = 60

SOURCE_IMAGE_DOWNLOAD_TIMEOUT = 15
MIN_IMAGE_BYTES = 5000

GEMINI_DELAY = 1.0
MAX_ARTICLE_TEXT = 12000

MAX_POST_PARAGRAPHS = 3

FRESH_DAYS = 90
HISTORICAL_YEAR_TOLERANCE = 3

DATABASE_READY = False
GEMINI_PRIMARY_DISABLED = False


def new_run_stats():
    return {
        "heavy_found": 0,
        "already_processed": 0,
        "new_articles": 0,
        "published": 0,
        "skipped": 0,
        "gemini_primary": 0,
        "gemini_fallback": 0,
        "images_downloaded": 0,
        "image_failures": 0,
        "post_rejected": 0,
        "technical_errors": 0,
        "telegram_errors": 0,
    }


RUN_STATS = new_run_stats()


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


def migrate_database(conn):
    print("[DATABASE] Checking database schema...")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            title TEXT,
            source TEXT,
            published TEXT,
            processed INTEGER DEFAULT 0,
            created_at TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT,
            error TEXT,
            error_message TEXT,
            stage TEXT,
            created_at TEXT
        )
        """
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
        "processed",
        "INTEGER DEFAULT 0",
    )

    add_column_if_missing(
        conn,
        "news",
        "created_at",
        "TEXT",
    )

    add_column_if_missing(
        conn,
        "images",
        "used",
        "INTEGER DEFAULT 0",
    )

    add_column_if_missing(
        conn,
        "images",
        "created_at",
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
        "TEXT",
    )

    add_column_if_missing(
        conn,
        "errors",
        "stage",
        "TEXT",
    )

    add_column_if_missing(
        conn,
        "errors",
        "created_at",
        "TEXT",
    )

    print("[DATABASE] Normalizing existing URLs...")

    rows = conn.execute(
        """
        SELECT id, url
        FROM news
        """
    ).fetchall()

    for row_id, url in rows:
        normalized = normalize_url(url)

        if normalized and normalized != url:
            conn.execute(
                """
                UPDATE news
                SET url = ?
                WHERE id = ?
                """,
                (
                    normalized,
                    row_id,
                ),
            )

    rows = conn.execute(
        """
        SELECT id, url
        FROM images
        """
    ).fetchall()

    for row_id, url in rows:
        normalized = normalize_url(url)

        if normalized and normalized != url:
            conn.execute(
                """
                UPDATE images
                SET url = ?
                WHERE id = ?
                """,
                (
                    normalized,
                    row_id,
                ),
            )

    print("[DATABASE] Checking duplicate news URLs...")
    remove_duplicate_news(conn)

    print("[DATABASE] Checking duplicate image URLs...")
    remove_duplicate_images(conn)

    try:
        print("[DATABASE] Creating unique index for news.url...")

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_news_url_unique
            ON news(url)
            """
        )
    except sqlite3.IntegrityError:
        remove_duplicate_news(conn)

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_news_url_unique
            ON news(url)
            """
        )

    try:
        print("[DATABASE] Creating unique index for images.url...")

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_images_url_unique
            ON images(url)
            """
        )
    except sqlite3.IntegrityError:
        remove_duplicate_images(conn)

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_images_url_unique
            ON images(url)
            """
        )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_news_processed
        ON news(processed)
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_errors_created_at
        ON errors(created_at)
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
        image_id = row[0]

        conn.execute(
            """
            UPDATE images
            SET used = ?
            WHERE id = ?
            """,
            (
                used,
                image_id,
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

    except Exception as exc:
        print(
            "[ERROR LOGGING FAILED] "
            f"{exc}"
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

    if path in (
        "/sports/nhl",
        "/sports/nhl/",
    ):
        return False

    return True


def extract_listing_title(anchor):
    title = (
        anchor.get_text(" ", strip=True)
        or anchor.get("aria-label", "")
        or anchor.get("title", "")
        or ""
    )

    return re.sub(
        r"\s+",
        " ",
        title,
    ).strip()


def load_news():
    print(
        f"[HEAVY] Loading source page: {SOURCE_URL}"
    )

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

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    result = []
    seen = set()

    for anchor in soup.find_all(
        "a",
        href=True,
    ):
        url = normalize_url(
            urljoin(
                response.url,
                anchor.get("href", ""),
            )
        )

        if (
            not is_heavy_article_url(url)
            or url in seen
        ):
            continue

        title = extract_listing_title(anchor)

        if not title:
            continue

        seen.add(url)

        result.append(
            {
                "url": url,
                "title": title,
                "source": "heavy.com",
                "published": "",
                "summary": "",
            }
        )

        if len(result) >= MAX_NEWS:
            break

    print(
        f"[HEAVY] Articles found: {len(result)}"
    )

    for index, item in enumerate(
        result,
        1,
    ):
        print(
            f"[HEAVY] {index}. "
            f"{item['title']} | "
            f"{item['url']}"
        )

    return result


# =========================================================
# ARTICLE
# =========================================================

def normalize_image_url(
    image_url,
    page_url,
):
    image_url = (image_url or "").strip()

    if not image_url:
        return ""

    image_url = urljoin(
        page_url,
        image_url,
    )

    parsed = urlparse(image_url)

    if parsed.scheme not in (
        "http",
        "https",
    ):
        return ""

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            "",
            parsed.query,
            "",
        )
    )


def add_image_candidate(
    candidates,
    image_url,
    source,
    page_url,
):
    image_url = normalize_image_url(
        image_url,
        page_url,
    )

    if not image_url:
        return

    candidates.append(
        (
            image_url,
            source,
        )
    )


def extract_jsonld_images(
    data,
    page_url,
):
    result = []

    if isinstance(data, dict):
        image = data.get("image")

        if isinstance(image, str):
            result.append(
                normalize_image_url(
                    image,
                    page_url,
                )
            )

        elif isinstance(image, dict):
            value = (
                image.get("url")
                or image.get("contentUrl")
                or ""
            )

            if value:
                result.append(
                    normalize_image_url(
                        value,
                        page_url,
                    )
                )

        elif isinstance(image, list):
            for value in image:
                if isinstance(value, str):
                    result.append(
                        normalize_image_url(
                            value,
                            page_url,
                        )
                    )
                elif isinstance(value, dict):
                    nested = (
                        value.get("url")
                        or value.get("contentUrl")
                        or ""
                    )

                    if nested:
                        result.append(
                            normalize_image_url(
                                nested,
                                page_url,
                            )
                        )

        for value in data.values():
            if isinstance(value, (dict, list)):
                result.extend(
                    extract_jsonld_images(
                        value,
                        page_url,
                    )
                )

    elif isinstance(data, list):
        for value in data:
            result.extend(
                extract_jsonld_images(
                    value,
                    page_url,
                )
            )

    return [
        value
        for value in result
        if value
    ]


def extract_source_images(
    soup,
    page_url,
):
    candidates = []

    # OpenGraph.
    for meta in soup.find_all(
        "meta",
    ):
        property_name = (
            meta.get("property")
            or meta.get("name")
            or ""
        ).lower()

        if property_name in (
            "og:image",
            "og:image:url",
            "og:image:secure_url",
            "twitter:image",
            "twitter:image:src",
        ):
            add_image_candidate(
                candidates,
                meta.get("content", ""),
                f"meta:{property_name}",
                page_url,
            )

    # Standard image links.
    for link in soup.find_all(
        "link",
        href=True,
    ):
        rel = [
            str(value).lower()
            for value in link.get("rel", [])
        ]

        if any(
            value in (
                "image_src",
                "image",
            )
            for value in rel
        ):
            add_image_candidate(
                candidates,
                link.get("href", ""),
                "link:image_src",
                page_url,
            )

    # JSON-LD.
    for script in soup.find_all(
        "script",
        attrs={
            "type": "application/ld+json",
        },
    ):
        raw = (
            script.string
            or script.get_text()
        )

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
                (
                    image_url,
                    "json-ld",
                )
            )

    unique = []
    seen = set()

    for image_url, source in candidates:
        if image_url in seen:
            continue

        seen.add(image_url)

        unique.append(
            (
                image_url,
                source,
            )
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
        response.headers.get(
            "Content-Type",
            "",
        )
        .split(
            ";",
            1,
        )[0]
        .strip()
        .lower()
    )

    # Some CDN responses omit Content-Type, so inspect common image signatures too.
    if not content_type.startswith("image/"):
        if content.startswith(
            b"\xff\xd8\xff"
        ):
            content_type = "image/jpeg"

        elif content.startswith(
            b"\x89PNG\r\n\x1a\n"
        ):
            content_type = "image/png"

        elif content.startswith(
            (
                b"GIF87a",
                b"GIF89a",
            )
        ):
            content_type = "image/gif"

        elif (
            content.startswith(b"RIFF")
            and content[8:12] == b"WEBP"
        ):
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

    extension = extension_map.get(
        content_type,
        ".img",
    )

    digest = hashlib.sha256(
        content
    ).hexdigest()[:16]

    path = os.path.join(
        tempfile.gettempdir(),
        f"nhl_{digest}{extension}",
    )

    try:
        with open(
            path,
            "wb",
        ) as image_file:
            image_file.write(content)
    except OSError as exc:
        print(
            "[IMAGE] Could not save image: "
            f"{exc}"
        )
        return None

    save_image(
        image_url,
        used=1,
    )

    print(
        "[IMAGE] Downloaded successfully: "
        f"{path}"
    )

    return path


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
            params={
                "key": GEMINI_API_KEY
            },
            json=payload,
            timeout=(
                10,
                GEMINI_TIMEOUT,
            ),
        )

    except requests.exceptions.ReadTimeout as exc:
        raise RuntimeError(
            f"Gemini read timeout after "
            f"{GEMINI_TIMEOUT}s"
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

    candidates = data.get(
        "candidates",
        [],
    )

    if not candidates:
        raise RuntimeError(
            "Gemini returned no candidates"
        )

    parts = (
        candidates[0]
        .get("content", {})
        .get("parts", [])
    )

    text = "".join(
        part.get("text", "")
        for part in parts
        if isinstance(
            part,
            dict,
        )
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


def clean_post(text):
    text = (text or "").strip()

    text = text.replace(
        "\r\n",
        "\n",
    ).replace(
        "\r",
        "\n",
    )

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
        for part in re.split(
            r"(?<=[.!?…])\s+",
            text.strip(),
        )
        if part.strip()
    ]


def build_short_paragraphs(text):
    """Normalize Gemini output and keep it readable without dropping content."""
    text = clean_post(text)

    raw_paragraphs = [
        re.sub(
            r"[ \t]+",
            " ",
            paragraph,
        ).strip()
        for paragraph in re.split(
            r"\n\s*\n+",
            text,
        )
        if paragraph.strip()
    ]

    if not raw_paragraphs:
        return ""

    paragraphs = []

    # If Gemini returned one large block, split it by sentences.
    if len(raw_paragraphs) == 1:
        sentences = split_sentences(
            raw_paragraphs[0]
        )

        if len(sentences) > 1:
            current = []

            for sentence in sentences:
                current.append(sentence)

                # Keep paragraphs short and mobile-friendly.
                if len(current) >= 2:
                    paragraphs.append(
                        " ".join(current)
                    )
                    current = []

            if current:
                paragraphs.append(
                    " ".join(current)
                )

        else:
            paragraphs = raw_paragraphs[:]

    else:
        paragraphs = raw_paragraphs[:]

    # Keep the layout to at most three paragraphs,
    # but NEVER truncate text.
    if len(paragraphs) > MAX_POST_PARAGRAPHS:
        paragraphs = (
            paragraphs[
                :MAX_POST_PARAGRAPHS - 1
            ]
            + [
                " ".join(
                    paragraphs[
                        MAX_POST_PARAGRAPHS - 1:
                    ]
                )
            ]
        )

    return "\n\n".join(
        paragraphs
    ).strip()


def format_telegram_post(text):
    """Apply restrained Telegram HTML formatting to the final post."""
    paragraphs = [
        paragraph.strip()
        for paragraph in text.split(
            "\n\n"
        )
        if paragraph.strip()
    ]

    if not paragraphs:
        return ""

    escaped = [
        html.escape(
            paragraph,
            quote=False,
        )
        for paragraph in paragraphs
    ]

    # The lead paragraph is bold; the rest stays clean and readable.
    escaped[0] = (
        f"<b>{escaped[0]}</b>"
    )

    return "\n\n".join(
        escaped
    )


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
        re.search(
            pattern,
            lowered,
        )
        for pattern in technical_patterns
    )


def validate_post_content(text):
    """Reject only clear garbage, non-Russian or technical output."""
    text = build_short_paragraphs(text)

    if not text:
        raise PostRejected(
            "Generated post is empty"
        )

    if "\ufffd" in text:
        raise PostRejected(
            "Generated post contains a replacement character"
        )

    if re.search(
        r"(.)\1{7,}",
        text,
    ):
        raise PostRejected(
            "Generated post contains repeated garbage characters"
        )

    if contains_technical_content(text):
        raise PostRejected(
            "Generated post contains technical information"
        )

    letters = re.findall(
        r"[A-Za-zА-Яа-яЁё]",
        text,
    )

    cyrillic = re.findall(
        r"[А-Яа-яЁё]",
        text,
    )

    if not letters:
        raise PostRejected(
            "Generated post contains no normal words"
        )

    russian_ratio = (
        len(cyrillic) / len(letters)
    )

    if russian_ratio < 0.45:
        raise PostRejected(
            "Generated post is not sufficiently Russian"
        )

    if any(
        ord(char) < 32
        and char not in "\n\r\t"
        for char in text
    ):
        raise PostRejected(
            "Generated post contains control characters"
        )

    return text


def generate_post_prompt(
    title,
    article,
):
    return f"""
Ты пишешь короткий пост для русскоязычного Telegram-канала о хоккейной новости.

Твоя задача — быстро объяснить читателю, что произошло и почему это важно. Не пересказывай статью целиком.

Правила:
- пиши только на русском языке;
- передавай только информацию из исходного материала;
- не добавляй факты от себя;
- пост должен относиться именно к этой новости;
- сначала мысленно определи одно главное событие или главный факт новости;
- первый абзац обязательно должен сразу сообщать это главное событие;
- после главного события выбери только самые важные детали, необходимые для понимания новости;
- обычно достаточно 2–3 коротких абзацев;
- если для полной передачи новости хватает 1–2 абзацев, не добавляй третий ради объема;
- каждый абзац должен быть небольшим и удобным для чтения с телефона;
- убирай второстепенные детали, длинный фон и информацию, которая не меняет смысл новости;
- не повторяй одну и ту же мысль разными словами; после написания проверь каждое предложение и удали повтор, если оно не добавляет новой информации;
- не добавляй вступление, вывод или фразу ради увеличения объема;
- закончи пост, как только новость полностью и понятно объяснена;
- не используй списки и подзаголовки;
- названия команд пиши без кавычек;
- не добавляй эмодзи;
- не используй HTML, Markdown или другие специальные обозначения форматирования;
- не используй служебные пометки, код, JSON, API-ответы, промпты или другую техническую информацию;
- не ориентируйся на фиксированное количество символов: важнее краткость, полнота и отсутствие повторов;
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


def generate_post(
    title,
    article,
):
    global GEMINI_PRIMARY_DISABLED

    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not configured"
        )

    prompt = generate_post_prompt(
        title,
        article,
    )

    models = []

    if not GEMINI_PRIMARY_DISABLED:
        models.append(
            (
                GEMINI_PRIMARY_MODEL,
                "PRIMARY",
            )
        )

    models.append(
        (
            GEMINI_FALLBACK_MODEL,
            "FALLBACK",
        )
    )

    last_error = None

    for model, label in models:
        print(
            f"[GEMINI {label}] Using {model}"
        )

        if label == "PRIMARY":
            RUN_STATS[
                "gemini_primary"
            ] += 1
        else:
            RUN_STATS[
                "gemini_fallback"
            ] += 1

        try:
            result = gemini_request(
                model,
                prompt,
            )

            result = clean_post(
                result
            )

            if result.upper() == "REJECT":
                raise PostRejected(
                    "Gemini could not produce a relevant Russian news post"
                )

            return validate_post_content(
                result
            )

        except PostRejected:
            raise

        except Exception as exc:
            last_error = exc

            print(
                f"[GEMINI {label} ERROR] "
                f"{exc}"
            )

            if (
                label == "PRIMARY"
                and is_gemini_temporary_error(
                    exc
                )
            ):
                GEMINI_PRIMARY_DISABLED = True

                print(
                    "[GEMINI] Primary disabled "
                    "for the remainder of this run; "
                    "using fallback model."
                )

                continue

            if label == "PRIMARY":
                continue

            break

    raise PostValidationServiceError(
        "Gemini content generation service failed: "
        f"{last_error}"
    )


# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(
    post,
    image_path=None,
):
    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "TOKEN is not configured"
        )

    if not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "CHAT_ID is not configured"
        )

    base_url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}"
    )

    formatted_post = format_telegram_post(
        post
    )

    if not formatted_post:
        raise RuntimeError(
            "Formatted Telegram post is empty"
        )

    if image_path:
        with open(
            image_path,
            "rb",
        ) as image_file:
            response = requests.post(
                f"{base_url}/sendPhoto",
                data={
                    "chat_id":
                        TELEGRAM_CHAT_ID,
                    "caption":
                        formatted_post,
                    "parse_mode":
                        "HTML",
                },
                files={
                    "photo": image_file
                },
                timeout=TELEGRAM_TIMEOUT,
            )

    else:
        response = requests.post(
            f"{base_url}/sendMessage",
            data={
                "chat_id":
                    TELEGRAM_CHAT_ID,
                "text":
                    formatted_post,
                "parse_mode":
                    "HTML",
            },
            timeout=TELEGRAM_TIMEOUT,
        )

    if response.status_code >= 400:
        raise RuntimeError(
            "Telegram HTTP "
            f"{response.status_code}: "
            f"{response.text[:1000]}"
        )


# =========================================================
# NEWS PROCESSING
# =========================================================

def process_news(
    item,
    index,
    total,
):
    print(
        f"[BOT] Processing {index}/{total}"
    )

    url = item["url"]

    try:
        save_news(item)

        article, source_images = fetch_article(
            url,
            item.get(
                "summary",
                "",
            ),
        )

        post = generate_post(
            item["title"],
            article,
        )

        image = None

        for (
            source_image_url,
            image_source,
        ) in source_images:
            image = download_source_image(
                source_image_url,
                url,
                image_source,
            )

            if image:
                break

        if image is None:
            RUN_STATS[
                "image_failures"
            ] += 1

            raise RuntimeError(
                "Could not download any image "
                "from the article page"
            )

        RUN_STATS[
            "images_downloaded"
        ] += 1

        send_telegram(
            post,
            image,
        )

        mark_processed(url)

        RUN_STATS[
            "published"
        ] += 1

        print(
            "[BOT] Published successfully"
        )

        return True

    except PostRejected as exc:
        RUN_STATS[
            "skipped"
        ] += 1

        RUN_STATS[
            "post_rejected"
        ] += 1

        print(
            f"[POST SKIPPED] {exc}"
        )

        log_error(
            url,
            str(exc),
            "post_rejected",
        )

        # A real content rejection is permanent:
        # do not retry it forever.
        mark_processed(url)

        return False

    except PostValidationServiceError as exc:
        RUN_STATS[
            "technical_errors"
        ] += 1

        print(
            f"[POST VALIDATION ERROR] "
            f"{exc}"
        )

        log_error(
            url,
            str(exc),
            "gemini_service",
        )

        # Service failures remain unprocessed
        # and will be retried later.
        return False

    except Exception as exc:
        message = str(exc)

        stage = "processing"

        if (
            "CHAT_ID" in message
            or "Telegram" in message
        ):
            stage = "telegram"

            RUN_STATS[
                "telegram_errors"
            ] += 1

            print(
                f"[TELEGRAM ERROR] "
                f"{message}"
            )

        elif (
            "Gemini" in message
            or "GEMINI" in message
        ):
            stage = "gemini"

            RUN_STATS[
                "technical_errors"
            ] += 1

        elif "image" in message.lower():
            stage = "image"

            RUN_STATS[
                "technical_errors"
            ] += 1

        elif (
            "article" in message.lower()
            or "HTTP" in message
        ):
            stage = "article"

            RUN_STATS[
                "technical_errors"
            ] += 1

        else:
            RUN_STATS[
                "technical_errors"
            ] += 1

        print(
            f"[FATAL ITEM ERROR] "
            f"{message}"
        )

        log_error(
            url,
            message,
            stage,
        )

        # Unexpected technical failures also
        # remain retryable.
        return False


# =========================================================
# RUN STATISTICS
# =========================================================

def print_run_stats():
    print("=" * 70)
    print("NHL NEWS BOT FINISHED")
    print("=" * 70)

    print(
        f"Found on Heavy:        "
        f"{RUN_STATS['heavy_found']}"
    )

    print(
        f"Already processed:     "
        f"{RUN_STATS['already_processed']}"
    )

    print(
        f"New articles:          "
        f"{RUN_STATS['new_articles']}"
    )

    print()

    print(
        f"Published:             "
        f"{RUN_STATS['published']}"
    )

    print(
        f"Skipped:               "
        f"{RUN_STATS['skipped']}"
    )

    print()

    print(
        f"Gemini primary:        "
        f"{RUN_STATS['gemini_primary']}"
    )

    print(
        f"Gemini fallback:       "
        f"{RUN_STATS['gemini_fallback']}"
    )

    print()

    print(
        f"Images downloaded:     "
        f"{RUN_STATS['images_downloaded']}"
    )

    print(
        f"Image failures:        "
        f"{RUN_STATS['image_failures']}"
    )

    print()

    print(
        f"Post rejected:         "
        f"{RUN_STATS['post_rejected']}"
    )

    print(
        f"Technical errors:      "
        f"{RUN_STATS['technical_errors']}"
    )

    print(
        f"Telegram errors:       "
        f"{RUN_STATS['telegram_errors']}"
    )

    print("=" * 70)


# =========================================================
# MAIN
# =========================================================

def main():
    global RUN_STATS

    RUN_STATS = new_run_stats()

    print("=" * 70)
    print("NHL NEWS BOT START")
    print("=" * 70)

    print(
        f"[CONFIG] Heavy articles limit: "
        f"{MAX_NEWS}"
    )

    print(
        f"[CONFIG] Gemini primary: "
        f"{GEMINI_PRIMARY_MODEL}"
    )

    print(
        f"[CONFIG] Gemini fallback: "
        f"{GEMINI_FALLBACK_MODEL}"
    )

    print(
        "[CONFIG] Images: article page only; "
        "no image search fallback"
    )

    print(
        "[CONFIG] Posts: concise digest, "
        "2-3 paragraphs, no hard length "
        "truncation, HTML formatting"
    )

    print("=" * 70)

    try:
        conn = get_db()
        conn.close()

    except Exception as exc:
        print(
            f"[DATABASE ERROR] {exc}"
        )

        log_error(
            "",
            str(exc),
            "database",
        )

        return

    try:
        items = load_news()

        print(
            "[HEAVY] Source loaded successfully"
        )

    except Exception as exc:
        print(
            f"[HEAVY ERROR] {exc}"
        )

        log_error(
            SOURCE_URL,
            str(exc),
            "source",
        )

        return

    RUN_STATS[
        "heavy_found"
    ] = len(items)

    new_items = [
        item
        for item in items
        if not is_processed(
            item["url"]
        )
    ]

    RUN_STATS[
        "already_processed"
    ] = (
        RUN_STATS["heavy_found"]
        - len(new_items)
    )

    RUN_STATS[
        "new_articles"
    ] = len(new_items)

    print(
        f"[HEAVY] Already processed: "
        f"{RUN_STATS['already_processed']}"
    )

    print(
        f"[HEAVY] New articles: "
        f"{RUN_STATS['new_articles']}"
    )

    if not new_items:
        print(
            "[BOT] No new articles. Nothing to publish."
        )

        print(
            "[BOT] Run completed successfully "
            "with no new articles."
        )

        print_run_stats()

        return

    for index, item in enumerate(
        new_items,
        1,
    ):
        process_news(
            item,
            index,
            len(new_items),
        )

    print_run_stats()


if __name__ == "__main__":
    main()
