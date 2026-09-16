import os
import re
import hashlib
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


def validate_post_structure(text):
    """Deterministic checks for the Telegram post format."""
    text = clean_post(text)
    length = len(text)

    if length < 400 or length > 600:
        raise PostRejected(
            f"Post length is {length} characters; required 400-600"
        )

    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", text)
        if paragraph.strip()
    ]

    if len(paragraphs) != 3:
        raise PostRejected(
            f"Post must contain exactly 3 paragraphs; found {len(paragraphs)}"
        )

    paragraph_lengths = [len(paragraph) for paragraph in paragraphs]

    if min(paragraph_lengths) < 80:
        raise PostRejected(
            "One of the paragraphs is too short"
        )

    if max(paragraph_lengths) > min(paragraph_lengths) * 1.8:
        raise PostRejected(
            "Paragraphs are not approximately equal in length"
        )

    if "«" in text or "»" in text:
        raise PostRejected(
            "Post contains Russian quotation marks"
        )

    if text.startswith("-") or text.startswith("•"):
        raise PostRejected(
            "Post looks like a list instead of a normal Telegram post"
        )

    return text


def generate_post_prompt(title, article):
    return f"""
Ты пишешь короткий пост для русскоязычного Telegram-канала про NHL.

Твоя задача — сделать точную и грамотную выжимку самого важного из материала.

ЖЁСТКИЕ ТРЕБОВАНИЯ:
1. Итоговый текст должен содержать от 400 до 600 символов включительно. Целься в 460-520 символов, чтобы не выйти за предел. Считай все символы, включая пробелы и знаки препинания.
2. Сделай ровно 3 органичных абзаца.
3. Каждый абзац должен быть примерно 140-180 символов; не делай один абзац заметно длиннее остальных.
4. Не растягивай текст ради достижения лимита. Убери всё второстепенное.
5. Сохрани главное событие, ключевые детали, цифры, имена и последствия, если они есть в материале.
6. Не добавляй ни одного факта, которого нет в исходном материале.
7. Не выдумывай цитаты и не меняй смысл существующих цитат.
8. Не переводи дословно. Пиши естественно по-русски, как живой автор Telegram-канала.
9. Не начинай с шаблонных фраз вроде «Стало известно», «Похоже, что», «Вот это поворот».
10. Названия хоккейных команд пиши без кавычек. Не используй «» вокруг названий команд.
11. Не используй списки, подзаголовки, эмодзи и служебные пометки.
12. Перед отправкой обязательно проверь русский язык: орфографию, пунктуацию, грамматику, согласование, падежи и естественность формулировок.
13. Если в исходнике есть сомнительная или противоречивая информация, не додумывай её. Передай только то, что прямо подтверждается материалом.

Заголовок статьи:
{title}

Материал статьи:
{article}

Верни только готовый текст поста. Никаких пояснений.
""".strip()


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

    def repair_prompt(previous_post):
        return f"""
Твой предыдущий вариант поста не прошёл техническую проверку.

Исправь ЕГО, а не пиши новый материал с нуля. Сохрани все важные факты, имена, цифры и смысл.

ЖЁСТКИЕ ОГРАНИЧЕНИЯ:
- итоговый текст: 400-600 символов включительно; ЦЕЛЬ — 460-520 символов;
- ровно 3 абзаца;
- каждый абзац примерно 140-180 символов;
- без кавычек «»;
- без списков, заголовков, эмодзи и пояснений;
- только русский текст поста;
- не добавляй факты, которых нет в исходнике;
- не растягивай текст: убирай второстепенные детали;
- проверь орфографию, пунктуацию и грамматику.

Заголовок:
{title}

Исходный материал:
{article}

Предыдущий вариант:
{previous_post}

Верни только исправленный пост.
""".strip()

    def generate_with_model(model, label, request_prompt):
        print(
            f"[GEMINI {label}] Using {model}"
        )

        result = gemini_request(
            model,
            request_prompt,
        )

        time.sleep(
            GEMINI_DELAY
        )

        return result

    # -----------------------------------------------------
    # PRIMARY MODEL
    # -----------------------------------------------------

    if not GEMINI_PRIMARY_DISABLED:
        try:
            result = generate_with_model(
                GEMINI_PRIMARY_MODEL,
                "PRIMARY",
                prompt,
            )

            try:
                return validate_post_structure(
                    result
                )

            except PostRejected as exc:
                print(
                    "[POST FORMAT ERROR] "
                    f"{exc}"
                )

                repaired = generate_with_model(
                    GEMINI_PRIMARY_MODEL,
                    "PRIMARY REPAIR",
                    repair_prompt(
                        clean_post(result)
                    ),
                )

                return validate_post_structure(
                    repaired
                )

        except PostRejected as exc:
            print(
                "[POST REPAIR FAILED] "
                f"{exc}"
            )
            raise

        except Exception as exc:
            print(
                "[GEMINI PRIMARY ERROR] "
                f"{exc}"
            )

            error_text = str(exc).lower()

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
                    "timeout",
                    "timed out",
                    "network error",
                    "connection timeout",
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

            else:
                raise

    # -----------------------------------------------------
    # FALLBACK MODEL
    # -----------------------------------------------------

    try:
        result = generate_with_model(
            GEMINI_FALLBACK_MODEL,
            "FALLBACK",
            prompt,
        )

        try:
            return validate_post_structure(
                result
            )

        except PostRejected as exc:
            print(
                "[POST FORMAT ERROR] Fallback output failed: "
                f"{exc}"
            )

            repaired = generate_with_model(
                GEMINI_FALLBACK_MODEL,
                "FALLBACK REPAIR",
                repair_prompt(
                    clean_post(result)
                ),
            )

            return validate_post_structure(
                repaired
            )

    except PostRejected as exc:
        print(
            "[POST REPAIR FAILED] Fallback: "
            f"{exc}"
        )
        raise


def check_post_language(post):
    """Use Gemini as a final grammar/spelling gate before publication."""
    prompt = f"""
Проверь готовый русский Telegram-пост ниже перед публикацией.

Нужно проверить:
- орфографию;
- пунктуацию;
- грамматику;
- согласование слов;
- падежи;
- очевидные опечатки;
- очевидные смысловые ошибки, возникшие из-за неправильной формулировки.

Не оценивай стиль и не предлагай улучшения, если текст просто можно написать иначе.
Проверяй только наличие реальных ошибок.

Если ошибок нет, ответь ровно: OK
Если есть хотя бы одна реальная ошибка, ответь ровно: ERROR

Пост:
{post}
""".strip()

    global GEMINI_PRIMARY_DISABLED

    result = None

    if not GEMINI_PRIMARY_DISABLED:
        try:
            result = gemini_request(
                GEMINI_PRIMARY_MODEL,
                prompt,
            )
        except Exception as exc:
            print(
                "[GEMINI LANGUAGE PRIMARY ERROR] "
                f"{exc}"
            )

            error_text = str(exc).lower()

            if any(
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
                    "404",
                    "not found",
                )
            ):
                GEMINI_PRIMARY_DISABLED = True

    if result is None:
        print(
            "[GEMINI LANGUAGE FALLBACK] Using "
            f"{GEMINI_FALLBACK_MODEL}"
        )

        try:
            result = gemini_request(
                GEMINI_FALLBACK_MODEL,
                prompt,
            )
        except Exception as exc:
            raise PostValidationServiceError(
                f"Language validation service failed: {exc}"
            ) from exc

    answer = clean_post(result).upper()

    if answer != "OK":
        raise PostRejected(
            "Gemini language validation failed: "
            f"{clean_post(result)[:200]}"
        )

    time.sleep(
        GEMINI_DELAY
    )

    print(
        "[POST VALIDATION] Language check: OK"
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

        try:
            check_post_language(post)
        except PostRejected as exc:
            print(
                "[POST SKIPPED] Validation failed: "
                f"{exc}"
            )

            log_error(
                url,
                str(exc),
                "post_validation",
            )

            mark_processed(url)
            return False

        except PostValidationServiceError as exc:
            print(
                "[POST VALIDATION ERROR] "
                f"{exc}"
            )

            log_error(
                url,
                str(exc),
                "post_validation_service",
            )

            return False

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
            "post" in message.lower()
            or "language" in message.lower()
        ):
            stage = "post_validation"

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
        "[CONFIG] Images: article page only; no image search fallback"
    )

    print("=" * 70)

    # -----------------------------------------------------
    # DATABASE INITIALIZATION
    # -----------------------------------------------------

    try:
        conn = get_db()
        conn.close()

    except Exception as exc:
        print(
            "[DATABASE ERROR] "
            f"{exc}"
        )

        log_error(
            "",
            str(exc),
            "database",
        )

        return

    # -----------------------------------------------------
    # HEAVY
    # -----------------------------------------------------

    try:
        items = load_news()

    except Exception as exc:
        print(
            "[HEAVY ERROR] "
            f"{exc}"
        )

        log_error(
            SOURCE_URL,
            str(exc),
            "source",
        )

        return

    # -----------------------------------------------------
    # NEW NEWS
    # -----------------------------------------------------

    new_items = [
        item
        for item in items
        if not is_processed(
            item["url"]
        )
    ]

    print(
        f"[HEAVY] New articles: "
        f"{len(new_items)}"
    )

    # -----------------------------------------------------
    # PROCESS
    # -----------------------------------------------------

    published = 0
    failed = 0

    for index, item in enumerate(
        new_items,
        1,
    ):
        if process_news(
            item,
            index,
            len(new_items),
        ):
            published += 1
        else:
            failed += 1

    # -----------------------------------------------------
    # FINISH
    # -----------------------------------------------------

    print("=" * 70)
    print("NHL NEWS BOT FINISHED")
    print(
        f"Published: {published}"
    )
    print(
        f"Failed: {failed}"
    )
    print("=" * 70)


if __name__ == "__main__":
    main()
