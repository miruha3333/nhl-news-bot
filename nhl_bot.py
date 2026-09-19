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


def image_attr_text(tag):
    values = []

    for attr in (
        "alt",
        "title",
        "class",
        "id",
        "data-testid",
        "data-ad-slot",
        "data-ad-unit",
        "aria-label",
    ):
        value = tag.get(attr, "")

        if isinstance(value, list):
            value = " ".join(str(item) for item in value)

        if value:
            values.append(str(value))

    return " ".join(values).lower()


def image_url_text(image_url):
    return (image_url or "").lower()


def nearest_context_text(tag, max_parents=4):
    parts = []
    current = tag

    for _ in range(max_parents):
        current = current.parent

        if current is None:
            break

        text = current.get_text(" ", strip=True)

        if text:
            parts.append(text[:1200])

    return " ".join(parts).lower()


def is_hard_ad_image(tag, image_url):
    """Return True for image candidates that are clearly promotional/advertising."""
    attr_text = image_attr_text(tag)
    url_text = image_url_text(image_url)
    context_text = nearest_context_text(tag, max_parents=3)

    hard_ad_patterns = (
        "advertisement",
        "advertising",
        "ad-container",
        "ad_container",
        "ad-slot",
        "ad_slot",
        "adsbygoogle",
        "sponsored",
        "sponsor",
        "promo",
        "promotional",
        "promotion",
        "newsletter",
        "subscribe",
        "subscription",
        "pick'em",
        "pickem",
        "contest",
        "giveaway",
        "sweepstakes",
        "betting",
        "sportsbook",
        "casino",
        "shop now",
        "shop",
        "store",
        "deal",
        "offer",
        "sale",
        "buy now",
        "click here",
        "enter heavy's",
        "enter heavys",
    )

    hard_ad_attr_patterns = (
        "ad-",
        "ad_",
        "ads-",
        "ads_",
        "advert",
        "sponsor",
        "promo",
        "promotional",
        "newsletter",
        "pickem",
        "pick'em",
        "contest",
        "sponsored",
    )

    if any(pattern in attr_text for pattern in hard_ad_patterns):
        return True

    if any(pattern in url_text for pattern in hard_ad_patterns):
        return True

    if any(pattern in attr_text for pattern in hard_ad_attr_patterns):
        return True

    # The example Heavy ad uses a normal <img> but exposes its promotional
    # nature through alt text and the surrounding block. Check the nearby
    # DOM text as a second hard exclusion signal.
    if any(pattern in context_text for pattern in hard_ad_patterns):
        return True

    return False


def image_is_social_candidate(tag, image_url):
    attr_text = image_attr_text(tag)
    url_text = image_url_text(image_url)
    context_text = nearest_context_text(tag, max_parents=5)

    social_patterns = (
        "twitter",
        "x.com",
        "twitter.com",
        "tweet",
        "t.co",
        "instagram",
        "instagram.com",
        "facebook",
        "facebook.com",
        "threads.net",
        "threads",
        "social-embed",
        "social_embed",
        "socialembed",
        "embed-social",
        "embed_social",
    )

    text = " ".join(
        (
            attr_text,
            url_text,
            context_text,
        )
    )

    return any(
        pattern in text
        for pattern in social_patterns
    )


def image_candidate_score(tag, image_url, position):
    """Score an image using deterministic DOM signals only."""
    attr_text = image_attr_text(tag)
    url_text = image_url_text(image_url)
    context_text = nearest_context_text(tag, max_parents=5)

    score = 0

    if image_is_social_candidate(tag, image_url):
        # Social screenshots are deliberately given a very strong priority.
        score += 1000

    positive_patterns = (
        ("twitter", 180),
        ("x.com", 180),
        ("tweet", 180),
        ("instagram", 180),
        ("facebook", 180),
        ("threads", 180),
        ("social", 140),
        ("figcaption", 120),
        ("figure", 80),
        ("article-image", 80),
        ("article_image", 80),
        ("featured-image", 60),
        ("featured_image", 60),
        ("hero-image", 50),
        ("hero_image", 50),
        ("getty", 50),
    )

    combined = " ".join(
        (
            attr_text,
            url_text,
            context_text,
        )
    )

    for pattern, points in positive_patterns:
        if pattern in combined:
            score += points

    # Keep article images ahead of generic decorative images, but never let
    # these modest bonuses override the strong social-embed preference.
    width = tag.get("width", "")
    height = tag.get("height", "")

    try:
        width_value = int(re.sub(r"[^0-9]", "", str(width)))
    except (TypeError, ValueError):
        width_value = 0

    try:
        height_value = int(re.sub(r"[^0-9]", "", str(height)))
    except (TypeError, ValueError):
        height_value = 0

    if width_value >= 500 or height_value >= 300:
        score += 25

    if width_value and width_value < 180:
        score -= 120

    if height_value and height_value < 120:
        score -= 120

    if position < 8:
        score += 30
    elif position > 40:
        score -= 20

    return score


def extract_article_body_images(soup, page_url):
    """Extract and rank real images found inside the Heavy article page."""
    candidates = []
    seen = set()

    for position, image_tag in enumerate(
        soup.find_all("img"),
        1,
    ):
        image_url = normalize_image_url(
            image_tag.get("src", "")
            or image_tag.get("data-src", "")
            or image_tag.get("data-lazy-src", ""),
            page_url,
        )

        if not image_url:
            srcset = (
                image_tag.get("srcset", "")
                or image_tag.get("data-srcset", "")
                or ""
            )

            if srcset:
                first_src = srcset.split(",", 1)[0].strip().split(" ", 1)[0]
                image_url = normalize_image_url(
                    first_src,
                    page_url,
                )

        if not image_url or image_url in seen:
            continue

        if is_hard_ad_image(
            image_tag,
            image_url,
        ):
            print(
                "[IMAGE] Hard ad exclusion: "
                f"{image_url} | "
                f"{image_attr_text(image_tag)[:220]}"
            )
            continue

        seen.add(image_url)

        score = image_candidate_score(
            image_tag,
            image_url,
            position,
        )

        source = (
            "article:social"
            if image_is_social_candidate(
                image_tag,
                image_url,
            )
            else "article:body"
        )

        candidates.append(
            (
                score,
                image_url,
                source,
            )
        )

    candidates.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    return [
        (image_url, f"{source}:score={score}")
        for score, image_url, source in candidates
    ]


def extract_source_images(soup, page_url):
    """Return article-body images first, then page metadata as a fallback."""
    candidates = extract_article_body_images(
        soup,
        page_url,
    )

    seen = {
        image_url
        for image_url, _source in candidates
    }

    metadata_candidates = []

    for meta in soup.find_all("meta"):
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
                metadata_candidates.append(
                    (image_url, "metadata:og:image")
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
                metadata_candidates.append(
                    (image_url, "metadata:twitter:image")
                )

    for link in soup.find_all("link"):
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
                metadata_candidates.append(
                    (image_url, "metadata:link:image_src")
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
            metadata_candidates.append(
                (image_url, "metadata:json-ld")
            )

    for image_url, source in metadata_candidates:
        if image_url in seen:
            continue

        # Metadata is a last-resort source. It is also checked against the
        # same URL-level ad patterns so a promotional OG image is not allowed
        # to re-enter the candidate list.
        if any(
            pattern in image_url_text(image_url)
            for pattern in (
                "advertisement",
                "sponsored",
                "promo",
                "contest",
                "pickem",
                "pick'em",
                "newsletter",
                "betting",
                "casino",
            )
        ):
            print(
                "[IMAGE] Hard ad exclusion (metadata): "
                f"{image_url}"
            )
            continue

        seen.add(image_url)
        candidates.append(
            (image_url, source)
        )

    return candidates


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

    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    prompt = generate_post_prompt(title, article)

    models = []

    if not GEMINI_PRIMARY_DISABLED:
        models.append((GEMINI_PRIMARY_MODEL, "PRIMARY"))

    models.append((GEMINI_FALLBACK_MODEL, "FALLBACK"))

    last_error = None

    for model, label in models:
        print(f"[GEMINI {label}] Using {model}")

        try:
            result = gemini_request(model, prompt)
            result = clean_post(result)

            if result.upper() == "REJECT":
                raise PostRejected(
                    "Gemini could not produce a relevant Russian news post"
                )

            return validate_post_content(result)

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

    raise PostValidationServiceError(
        f"Gemini content generation service failed: {last_error}"
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
