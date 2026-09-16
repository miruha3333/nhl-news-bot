import os
import re
import hashlib
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlunparse

import feedparser
import requests
from bs4 import BeautifulSoup
from ddgs import DDGS


RSS_URL = os.getenv("RSS_URL", "").strip()
TELEGRAM_TOKEN = os.getenv("TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

DATABASE_FILE = "nhl_bot.db"

MAX_RSS_ENTRIES = 30

GEMINI_PRIMARY_MODEL = "gemini-3.5-flash"
GEMINI_FALLBACK_MODEL = "gemini-3.1-flash-lite"

GEMINI_TIMEOUT = 90
TELEGRAM_TIMEOUT = 60

IMAGE_RESULTS_LIMIT = 30
IMAGE_DOWNLOAD_TIMEOUT = 20
SOURCE_IMAGE_DOWNLOAD_TIMEOUT = 15
MIN_IMAGE_BYTES = 5000

GEMINI_DELAY = 1.0
MAX_ARTICLE_TEXT = 12000

FRESH_DAYS = 90
HISTORICAL_YEAR_TOLERANCE = 3

DATABASE_READY = False
GEMINI_PRIMARY_DISABLED = False


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
        "INTEGER NOT NULL DEFAULT 0",
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
        "TEXT",
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
    # NORMALIZE URLS
    # -----------------------------------------------------

    print(
        "[DATABASE] Normalizing existing URLs..."
    )

    normalize_existing_urls(
        conn,
        "news",
    )

    normalize_existing_urls(
        conn,
        "images",
    )

    # -----------------------------------------------------
    # REMOVE DUPLICATES
    # -----------------------------------------------------

    print(
        "[DATABASE] Checking duplicate news URLs..."
    )

    remove_duplicate_news(conn)

    print(
        "[DATABASE] Checking duplicate image URLs..."
    )

    remove_duplicate_images(conn)

    # -----------------------------------------------------
    # UNIQUE INDEXES
    # -----------------------------------------------------

    print(
        "[DATABASE] Creating unique index for news.url..."
    )

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
        idx_news_url_unique
        ON news(url)
        """
    )

    print(
        "[DATABASE] Creating unique index for images.url..."
    )

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
        idx_images_url_unique
        ON images(url)
        """
    )

    conn.commit()

    print(
        "[DATABASE] Schema check complete."
    )


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
        created_at = datetime.now(
            timezone.utc
        ).isoformat()

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
        created_at = datetime.now(
            timezone.utc
        ).isoformat()

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

        fields = []
        values = []

        if "url" in columns:
            fields.append("url")
            values.append(url)

        if "error" in columns:
            fields.append("error")
            values.append(message)

        # Поддерживаем старую схему БД.
        if "error_message" in columns:
            fields.append("error_message")
            values.append(message)

        if "stage" in columns:
            fields.append("stage")
            values.append(stage)

        if "created_at" in columns:
            fields.append("created_at")
            values.append(
                datetime.now(
                    timezone.utc
                ).isoformat()
            )

        placeholders = ", ".join(
            ["?"] * len(fields)
        )

        field_sql = ", ".join(
            fields
        )

        conn.execute(
            f"""
            INSERT INTO errors (
                {field_sql}
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
            f"[ERROR LOGGER FAILURE] {exc}"
        )


# =========================================================
# RSS
# =========================================================

def load_rss():
    if not RSS_URL:
        raise RuntimeError(
            "RSS_URL is not configured"
        )

    print(
        "[RSS] Loading feed..."
    )

    feed = feedparser.parse(
        RSS_URL
    )

    entries = list(
        feed.entries
    )[:MAX_RSS_ENTRIES]

    print(
        f"[RSS] Entries received: "
        f"{len(entries)}"
    )

    result = []

    for entry in entries:
        url = normalize_url(
            entry.get("link", "")
        )

        if not url:
            continue

        summary = BeautifulSoup(
            entry.get(
                "summary",
                "",
            ),
            "html.parser",
        ).get_text(
            " ",
            strip=True,
        )

        result.append(
            {
                "url": url,
                "title": entry.get(
                    "title",
                    "",
                ).strip(),
                "source": urlparse(
                    url
                ).netloc,
                "published": entry.get(
                    "published",
                    entry.get(
                        "updated",
                        "",
                    ),
                ),
                "summary": summary,
            }
        )

    return result


# =========================================================
# ARTICLE / SOURCE IMAGE
# =========================================================

def normalize_image_url(
    image_url,
    page_url,
):
    image_url = (
        image_url or ""
    ).strip()

    if not image_url:
        return ""

    image_url = urljoin(
        page_url,
        image_url,
    )

    parsed = urlparse(
        image_url
    )

    if parsed.scheme not in (
        "http",
        "https",
    ):
        return ""

    return image_url


def extract_jsonld_images(
    value,
    page_url,
):
    images = []

    if isinstance(value, str):
        normalized = normalize_image_url(
            value,
            page_url,
        )

        if normalized:
            images.append(
                normalized
            )

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

        for item in value.get(
            "@graph",
            [],
        ):
            images.extend(
                extract_jsonld_images(
                    item,
                    page_url,
                )
            )

    return images


def extract_source_images(
    soup,
    page_url,
):
    candidates = []

    for meta in soup.find_all(
        "meta"
    ):
        prop = (
            meta.get(
                "property",
                "",
            )
            or meta.get(
                "name",
                "",
            )
        ).lower().strip()

        if prop in (
            "og:image",
            "og:image:url",
            "og:image:secure_url",
        ):
            image_url = normalize_image_url(
                meta.get(
                    "content",
                    "",
                ),
                page_url,
            )

            if image_url:
                candidates.append(
                    (
                        image_url,
                        "og:image",
                    )
                )

        elif prop in (
            "twitter:image",
            "twitter:image:src",
        ):
            image_url = normalize_image_url(
                meta.get(
                    "content",
                    "",
                ),
                page_url,
            )

            if image_url:
                candidates.append(
                    (
                        image_url,
                        "twitter:image",
                    )
                )

    for link in soup.find_all(
        "link"
    ):
        rel = [
            str(value).lower()
            for value in link.get(
                "rel",
                [],
            )
        ]

        if "image_src" in rel:
            image_url = normalize_image_url(
                link.get(
                    "href",
                    "",
                ),
                page_url,
            )

            if image_url:
                candidates.append(
                    (
                        image_url,
                        "link:image_src",
                    )
                )

    for script in soup.find_all(
        "script",
        attrs={
            "type":
                "application/ld+json"
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

            data = json.loads(
                raw
            )

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

        seen.add(
            image_url
        )

        unique.append(
            (
                image_url,
                source,
            )
        )

    return unique


def is_social_post_url(url):
    host = urlparse(
        url
    ).netloc.lower()

    host = host.split(
        ":",
        1,
    )[0]

    return host in {
        "x.com",
        "twitter.com",
        "mobile.twitter.com",
        "www.x.com",
        "www.twitter.com",
    }


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
        ]
    }

    response = requests.post(
        url,
        params={
            "key":
                GEMINI_API_KEY
        },
        json=payload,
        timeout=GEMINI_TIMEOUT,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            "Gemini HTTP "
            f"{response.status_code}: "
            f"{response.text[:1000]}"
        )

    data = response.json()

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
        .get(
            "content",
            {},
        )
        .get(
            "parts",
            [],
        )
    )

    text = "".join(
        part.get(
            "text",
            "",
        )
        for part in parts
    ).strip()

    if not text:
        raise RuntimeError(
            "Gemini returned an empty response"
        )

    return text


def clean_post(text):
    text = text.strip()

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


def generate_post(
    title,
    article,
):
    global GEMINI_PRIMARY_DISABLED

    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not configured"
        )

    prompt = f"""
Ты пишешь пост для русскоязычного Telegram-канала про NHL.

Сделай из материала ниже живой авторский пост на русском языке.

Не переводи дословно.
Не добавляй фактов, которых нет в материале.
Не выдумывай цитаты.
Не начинай с шаблонных фраз вроде:
«Стало известно»,
«Похоже, что»,
«Вот это поворот».

Пиши естественно, как человек, который следит за NHL и делится новостью с аудиторией.

Заголовок:
{title}

Материал:
{article}

Верни только готовый текст поста.
Без пояснений.
Без служебных комментариев.
Без «Пост:».
""".strip()

    if not GEMINI_PRIMARY_DISABLED:
        try:
            result = gemini_request(
                GEMINI_PRIMARY_MODEL,
                prompt,
            )

            time.sleep(
                GEMINI_DELAY
            )

            return clean_post(
                result
            )

        except Exception as exc:
            print(
                "[GEMINI PRIMARY ERROR] "
                f"{exc}"
            )

            error_text = str(
                exc
            ).lower()

            disable_primary = any(
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
                )
            )

            if disable_primary:
                GEMINI_PRIMARY_DISABLED = True

                print(
                    "[GEMINI] Primary disabled for the remainder "
                    "of this run; using fallback model."
                )

            elif (
                "404" in error_text
                or "not found" in error_text
            ):
                GEMINI_PRIMARY_DISABLED = True

                print(
                    "[GEMINI] Primary model unavailable; "
                    "using fallback model."
                )

    print(
        "[GEMINI FALLBACK] Using "
        f"{GEMINI_FALLBACK_MODEL}"
    )

    try:
        result = gemini_request(
            GEMINI_FALLBACK_MODEL,
            prompt,
        )

    except Exception as exc:
        print(
            "[GEMINI FALLBACK ERROR] "
            f"{exc}"
        )

        raise

    time.sleep(
        GEMINI_DELAY
    )

    return clean_post(
        result
    )


# =========================================================
# IMAGE SEARCH
# =========================================================

def parse_date_year(value):
    match = re.search(
        r"\b(20\d{2})\b",
        value or "",
    )

    if match:
        return int(
            match.group(1)
        )

    return None


def image_score(
    result,
    query,
    historical_year=None,
):
    title = (
        result.get(
            "title",
            "",
        )
        or ""
    ).lower()

    source = (
        result.get(
            "source",
            "",
        )
        or ""
    ).lower()

    image_url = (
        result.get(
            "image",
            "",
        )
        or ""
    )

    score = 0

    words = re.findall(
        r"[a-zA-Z0-9À-ÿ]+",
        query,
    )

    query_words = [
        word.lower()
        for word in words
        if len(word) > 2
    ]

    score += min(
        50,
        sum(
            5
            for word in query_words
            if word in title
        ),
    )

    if source:
        score += 3

    if image_url:
        score += 5

    result_year = parse_date_year(
        result.get(
            "title",
            "",
        )
    )

    if (
        historical_year
        and result_year
    ):
        score += max(
            0,
            20
            - abs(
                result_year
                - historical_year
            ) * 5,
        )

    return score


def download_source_image(
    image_url,
    article_url,
    image_source,
):
    if not image_url:
        return None

    print(
        "[IMAGE] Trying source page image: "
        f"{image_source}"
    )

    print(
        "[IMAGE] URL: "
        f"{image_url}"
    )

    try:
        response = requests.get(
            image_url,
            headers={
                "User-Agent":
                    "Mozilla/5.0 "
                    "(NHLNewsBot/1.0)",
                "Referer":
                    article_url,
            },
            timeout=SOURCE_IMAGE_DOWNLOAD_TIMEOUT,
        )

        response.raise_for_status()

        content_type = (
            response.headers
            .get(
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

        if not content_type.startswith(
            "image/"
        ):
            print(
                "[IMAGE] Source URL is not an image: "
                f"{content_type or 'unknown'}"
            )

            return None

        if len(response.content) < MIN_IMAGE_BYTES:
            print(
                "[IMAGE] Source image is too small: "
                f"{len(response.content)} bytes"
            )

            return None

        suffix = ".jpg"

        if "png" in content_type:
            suffix = ".png"

        elif "webp" in content_type:
            suffix = ".webp"

        elif "gif" in content_type:
            suffix = ".gif"

        elif "jpeg" in content_type:
            suffix = ".jpg"

        filename = (
            "nhl_source_"
            + hashlib.md5(
                image_url.encode(
                    "utf-8"
                )
            ).hexdigest()
            + suffix
        )

        path = os.path.join(
            tempfile.gettempdir(),
            filename,
        )

        with open(
            path,
            "wb",
        ) as image_file:
            image_file.write(
                response.content
            )

        save_image(
            image_url,
            1,
        )

        print(
            "[IMAGE] Downloaded from source: "
            f"{path}"
        )

        return path

    except Exception as exc:
        print(
            "[IMAGE SOURCE ERROR] "
            f"{exc}"
        )

        return None


def download_image(
    search_query,
    historical_year=None,
):
    print(
        f"[IMAGE] Searching: "
        f"{search_query}"
    )

    try:
        with DDGS() as ddgs:
            results = list(
                ddgs.images(
                    search_query,
                    max_results=IMAGE_RESULTS_LIMIT,
                )
            )

    except Exception as exc:
        print(
            f"[IMAGE SEARCH ERROR] "
            f"{exc}"
        )

        return None

    if not results:
        print(
            "[IMAGE] No results found."
        )

        return None

    scored = sorted(
        results,
        key=lambda result:
            image_score(
                result,
                search_query,
                historical_year,
            ),
        reverse=True,
    )

    for candidate in scored:
        image_url = (
            candidate.get(
                "image"
            )
            or candidate.get(
                "thumbnail"
            )
        )

        if not image_url:
            continue

        print(
            "[IMAGE] Trying candidate:"
        )

        print(
            "[IMAGE] Title: "
            f"{candidate.get('title', '')}"
        )

        print(
            "[IMAGE] Source: "
            f"{candidate.get('source', '')}"
        )

        print(
            "[IMAGE] Score: "
            f"{image_score(candidate, search_query, historical_year)}"
        )

        try:
            response = requests.get(
                image_url,
                headers={
                    "User-Agent":
                        "Mozilla/5.0"
                },
                timeout=IMAGE_DOWNLOAD_TIMEOUT,
            )

            response.raise_for_status()

            content_type = (
                response.headers
                .get(
                    "Content-Type",
                    "",
                )
            )

            if not content_type.startswith(
                "image/"
            ):
                continue

            suffix = ".jpg"

            if "png" in content_type:
                suffix = ".png"

            filename = (
                "nhl_"
                + hashlib.md5(
                    image_url.encode()
                ).hexdigest()
                + suffix
            )

            path = os.path.join(
                tempfile.gettempdir(),
                filename,
            )

            with open(
                path,
                "wb",
            ) as image_file:
                image_file.write(
                    response.content
                )

            save_image(
                image_url,
                1,
            )

            print(
                "[IMAGE] Downloaded: "
                f"{path}"
            )

            return path

        except Exception as exc:
            print(
                f"[IMAGE ERROR] "
                f"{exc}"
            )

    print(
        "[IMAGE] No usable image found."
    )

    return None


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
                        post,
                },
                files={
                    "photo":
                        image_file
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
                    post,
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
# PROCESS NEWS
# =========================================================

def process_news(
    item,
    index,
    total,
):
    print(
        f"[BOT] Processing "
        f"{index}/{total}"
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

        if is_social_post_url(url):
            print(
                "[IMAGE] Social/X/Twitter source detected; "
                "using image search."
            )

            image = download_image(
                item["title"]
            )

        else:
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
                print(
                    "[IMAGE] No usable image found on source page; "
                    "falling back to image search."
                )

                image = download_image(
                    item["title"]
                )

        send_telegram(
            post,
            image,
        )

        mark_processed(
            url
        )

        print(
            "[BOT] Published successfully"
        )

        return True

    except Exception as exc:
        message = str(exc)

        stage = "processing"

        if (
            "CHAT_ID" in message
            or "Telegram" in message
        ):
            stage = "telegram"

            print(
                "[TELEGRAM ERROR] "
                f"{message}"
            )

        elif (
            "Gemini" in message
            or "GEMINI" in message
        ):
            stage = "gemini"

        elif (
            "image" in message.lower()
        ):
            stage = "image"

        elif (
            "article" in message.lower()
            or "HTTP" in message
        ):
            stage = "article"

        print(
            "[FATAL ITEM ERROR] "
            f"{message}"
        )

        log_error(
            url,
            message,
            stage,
        )

        return False


# =========================================================
# MAIN
# =========================================================

def main():
    print("=" * 70)
    print(
        "NHL NEWS BOT START"
    )
    print("=" * 70)

    print(
        f"[CONFIG] RSS entries limit: "
        f"{MAX_RSS_ENTRIES}"
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
        "[CONFIG] Images: source page first; "
        "X/Twitter + source fallback use image search"
    )

    print("=" * 70)

    try:
        get_db().close()

        items = load_rss()

    except Exception as exc:
        print(
            "[STARTUP ERROR] "
            f"{exc}"
        )

        log_error(
            "",
            exc,
            "startup",
        )

        return

    new_items = []

    for item in items:
        try:
            if not is_processed(
                item["url"]
            ):
                new_items.append(
                    item
                )

        except Exception as exc:
            print(
                "[DATABASE CHECK ERROR] "
                f"{exc}"
            )

            log_error(
                item.get(
                    "url",
                    "",
                ),
                exc,
                "database",
            )

    print(
        f"[RSS] New news: "
        f"{len(new_items)}"
    )

    if not new_items:
        print(
            "[BOT] Nothing to publish."
        )

        print("=" * 70)
        print(
            "NHL NEWS BOT FINISHED"
        )
        print(
            "Published: 0"
        )
        print(
            "Failed: 0"
        )
        print("=" * 70)

        return

    published = 0
    failed = 0

    total = len(
        new_items
    )

    for index, item in enumerate(
        new_items,
        start=1,
    ):
        success = process_news(
            item,
            index,
            total,
        )

        if success:
            published += 1

        else:
            failed += 1

    print("=" * 70)
    print(
        "NHL NEWS BOT FINISHED"
    )
    print(
        f"Published: {published}"
    )
    print(
        f"Failed: {failed}"
    )
    print("=" * 70)


if __name__ == "__main__":
    main()
