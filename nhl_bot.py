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
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
ANYMODEL_API_KEY = os.getenv("ANYMODEL_API_KEY", "").strip()
HF_TOKEN = os.getenv("HF_TOKEN", "").strip()

DATABASE_FILE = "nhl_bot.db"

MAX_NEWS = 30

GEMINI_PRIMARY_MODEL = "gemini-3.5-flash"
GEMINI_FALLBACK_MODEL = "gemini-3.5-flash-lite"
OPENROUTER_MODEL = "openrouter/free"
ANYMODEL_MODEL = "am/gpt-oss-20b"
HF_MODEL = "Qwen/Qwen3.5-27B"

GEMINI_TIMEOUT = 45
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
    """All generation providers rejected the news or could not produce valid text."""


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


def extract_body_image_url(img, page_url):
    """Return the most useful image URL exposed by a body <img> element."""
    attributes = (
        "src",
        "data-src",
        "data-lazy-src",
        "data-original",
        "data-image",
        "data-url",
    )

    for attribute in attributes:
        value = img.get(attribute, "")
        normalized = normalize_image_url(
            value,
            page_url,
        )
        if normalized:
            return normalized

    srcset = (
        img.get("srcset", "")
        or img.get("data-srcset", "")
        or ""
    ).strip()

    if srcset:
        # Prefer the largest declared source from srcset.
        candidates = []
        for part in srcset.split(","):
            tokens = part.strip().split()
            if not tokens:
                continue

            url = normalize_image_url(
                tokens[0],
                page_url,
            )
            if not url:
                continue

            score = 0
            if len(tokens) > 1:
                descriptor = tokens[1].strip().lower()
                match = re.match(r"(\d+)w", descriptor)
                if match:
                    score = int(match.group(1))
                else:
                    match = re.match(r"([0-9.]+)x", descriptor)
                    if match:
                        score = int(float(match.group(1)) * 1000)

            candidates.append((score, url))

        if candidates:
            candidates.sort(
                key=lambda item: item[0],
                reverse=True,
            )
            return candidates[0][1]

    return ""


def image_dimension_score(img):
    """Estimate whether an <img> is a real article photo rather than an icon."""
    width = img.get("width", "")
    height = img.get("height", "")

    try:
        width = int(re.sub(r"[^0-9]", "", str(width)))
    except (TypeError, ValueError):
        width = 0

    try:
        height = int(re.sub(r"[^0-9]", "", str(height)))
    except (TypeError, ValueError):
        height = 0

    if width >= 300 and height >= 200:
        return 3

    if width >= 300 or height >= 200:
        return 2

    if width >= 150 or height >= 150:
        return 1

    return 0


def is_probable_non_article_image(img):
    """Filter obvious logos, icons, avatars and tracking images."""
    values = []

    for attribute in (
        "alt",
        "class",
        "id",
        "title",
        "aria-label",
    ):
        value = img.get(attribute, "")
        if isinstance(value, list):
            value = " ".join(str(item) for item in value)
        values.append(str(value).lower())

    marker_text = " ".join(values)

    blocked_markers = (
        "logo",
        "icon",
        "avatar",
        "author-photo",
        "author_image",
        "profile-photo",
        "profile_image",
        "placeholder",
        "sprite",
        "social",
        "facebook",
        "twitter",
        "instagram",
        "pinterest",
    )

    return any(
        marker in marker_text
        for marker in blocked_markers
    )


def extract_source_images(soup, page_url):
    """Extract images from the actual article body, not the page's OG/preview image."""
    candidates = []
    seen = set()

    article_roots = []

    # Heavy may expose the article body using any of these common structures.
    selectors = (
        'article',
        '[itemprop="articleBody"]',
        'main article',
        'main',
        '.article-content',
        '.article-body',
        '.entry-content',
        '.post-content',
        '.single-post-content',
    )

    for selector in selectors:
        try:
            for root in soup.select(selector):
                if root not in article_roots:
                    article_roots.append(root)
        except Exception:
            continue

    # Prefer the most specific article-body container. If it is unavailable,
    # the broader article/main containers still give us body images.
    for root in article_roots:
        for img in root.find_all("img"):
            if is_probable_non_article_image(img):
                continue

            image_url = extract_body_image_url(
                img,
                page_url,
            )

            if not image_url or image_url in seen:
                continue

            dimension_score = image_dimension_score(img)

            # Ignore very small images when dimensions are explicitly known.
            width = img.get("width", "")
            height = img.get("height", "")
            try:
                width_value = int(re.sub(r"[^0-9]", "", str(width)))
            except (TypeError, ValueError):
                width_value = 0
            try:
                height_value = int(re.sub(r"[^0-9]", "", str(height)))
            except (TypeError, ValueError):
                height_value = 0

            if (
                width_value
                and height_value
                and width_value < 120
                and height_value < 120
            ):
                continue

            seen.add(image_url)
            candidates.append(
                (
                    image_url,
                    "article-body",
                    dimension_score,
                )
            )

    # Put images with explicit large dimensions first, while preserving the
    # original order among images with the same score. This keeps the first
    # substantial article photo as the normal choice.
    candidates.sort(
        key=lambda item: item[2],
        reverse=True,
    )

    return [
        (image_url, image_source)
        for image_url, image_source, _score in candidates
    ]


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
            "[IMAGE] Article body image candidates: "
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
    """Download an image directly from an image found inside the Heavy article body."""
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



def openai_compatible_request(
    provider,
    url,
    api_key,
    model,
    prompt,
):
    if not api_key:
        raise RuntimeError(f"{provider} API key is not configured")

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        "temperature": 0.2,
        "max_tokens": 1000,
    }

    try:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=(10, GEMINI_TIMEOUT),
        )
    except requests.exceptions.ReadTimeout as exc:
        raise RuntimeError(
            f"{provider} read timeout after {GEMINI_TIMEOUT}s"
        ) from exc
    except requests.exceptions.ConnectTimeout as exc:
        raise RuntimeError(f"{provider} connection timeout") from exc
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"{provider} network error: {exc}") from exc

    if response.status_code >= 400:
        raise RuntimeError(
            f"{provider} HTTP {response.status_code}: "
            f"{response.text[:1000]}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{provider} returned invalid JSON") from exc

    choices = data.get("choices", [])
    if not choices:
        raise RuntimeError(f"{provider} returned no choices")

    message = choices[0].get("message", {})
    text = message.get("content", "")

    if isinstance(text, list):
        text = "".join(
            part.get("text", "")
            for part in text
            if isinstance(part, dict)
        )

    text = str(text or "").strip()

    if not text:
        raise RuntimeError(f"{provider} returned an empty response")

    return text


def extract_reject_reason(text):
    cleaned = (text or "").strip()
    match = re.match(r"^REJECT\s*:\s*(.+)$", cleaned, flags=re.IGNORECASE | re.DOTALL)

    if match:
        reason = re.sub(r"\s+", " ", match.group(1)).strip()
        return reason[:500] or "No reason provided"

    if cleaned.upper() == "REJECT":
        return "Model returned REJECT without a reason"

    return ""

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
- если исходный материал невозможно нормально превратить в русскую новостную выжимку, верни в формате REJECT: краткая причина (не более 20 слов);
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

    prompt = generate_post_prompt(title, article)

    providers = []

    if GEMINI_API_KEY and not GEMINI_PRIMARY_DISABLED:
        providers.append((
            "GEMINI PRIMARY",
            GEMINI_PRIMARY_MODEL,
            lambda: gemini_request(GEMINI_PRIMARY_MODEL, prompt),
            "gemini",
        ))

    if GEMINI_API_KEY:
        providers.append((
            "GEMINI FALLBACK",
            GEMINI_FALLBACK_MODEL,
            lambda: gemini_request(GEMINI_FALLBACK_MODEL, prompt),
            "gemini_fallback",
        ))

    if OPENROUTER_API_KEY:
        providers.append((
            "OPENROUTER",
            OPENROUTER_MODEL,
            lambda: openai_compatible_request(
                "OpenRouter",
                "https://openrouter.ai/api/v1/chat/completions",
                OPENROUTER_API_KEY,
                OPENROUTER_MODEL,
                prompt,
            ),
            "openrouter",
        ))

    if ANYMODEL_API_KEY:
        providers.append((
            "ANYMODEL",
            ANYMODEL_MODEL,
            lambda: openai_compatible_request(
                "AnyModel",
                "https://anymodel.org/v1/chat/completions",
                ANYMODEL_API_KEY,
                ANYMODEL_MODEL,
                prompt,
            ),
            "anymodel",
        ))

    if HF_TOKEN:
        providers.append((
            "HUGGING FACE",
            HF_MODEL,
            lambda: openai_compatible_request(
                "Hugging Face",
                "https://router.huggingface.co/v1/chat/completions",
                HF_TOKEN,
                HF_MODEL,
                prompt,
            ),
            "huggingface",
        ))

    if not providers:
        raise PostValidationServiceError(
            "No AI generation provider is configured"
        )

    errors = []
    rejections = []

    for label, model, request_fn, provider_key in providers:
        print(f"[AI {label}] Using {model}")

        try:
            raw_result = request_fn()
            reject_reason = extract_reject_reason(raw_result)

            if reject_reason:
                rejections.append(f"{label}: {reject_reason}")
                print(
                    f"[AI {label} REJECT] {reject_reason}"
                )
                continue

            result = clean_post(raw_result)
            validated = validate_post_content(result)

            print(
                f"[AI {label}] Post generated successfully"
            )
            return validated

        except PostRejected as exc:
            reason = str(exc)
            rejections.append(f"{label}: {reason}")
            print(
                f"[AI {label} VALIDATION REJECT] {reason}"
            )
            continue

        except Exception as exc:
            errors.append(f"{label}: {exc}")
            print(
                f"[AI {label} ERROR] {exc}"
            )

            if label == "GEMINI PRIMARY" and is_gemini_temporary_error(exc):
                GEMINI_PRIMARY_DISABLED = True
                print(
                    "[GEMINI] Primary disabled for the remainder of this run; "
                    "continuing through the fallback chain."
                )

            continue

    if rejections and not errors:
        raise PostRejected(
            "All AI providers rejected the news. Reasons: "
            + " | ".join(rejections)
        )

    if rejections or errors:
        details = []
        if rejections:
            details.append(
                "rejections=" + " | ".join(rejections)
            )
        if errors:
            details.append(
                "errors=" + " | ".join(errors)
            )

        raise PostValidationServiceError(
            "All AI generation providers failed. "
            + " ; ".join(details)
        )

    raise PostValidationServiceError(
        "AI generation chain ended without a usable post"
    )


# =========================================================
# TELEGRAM
# =========================================================

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

    if image_path:
        with open(image_path, "rb") as image_file:
            response = requests.post(
                f"{base_url}/sendPhoto",
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "caption": formatted_post,
                    "parse_mode": "HTML",
                },
                files={"photo": image_file},
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

        elif any(
            marker in message
            for marker in ("Gemini", "GEMINI", "OpenRouter", "AnyModel", "Hugging Face")
        ):
            stage = "ai_generation"

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
    print(f"[CONFIG] OpenRouter fallback: {OPENROUTER_MODEL}")
    print(f"[CONFIG] AnyModel fallback: {ANYMODEL_MODEL}")
    print(f"[CONFIG] Hugging Face fallback: {HF_MODEL}")
    print("[CONFIG] Images: article body only; no image search fallback")
    print("[CONFIG] Posts: concise, 2-4 paragraphs, max 850 chars, HTML formatting")
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

    print(f"[HEAVY] New articles: {len(new_items)}")

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
