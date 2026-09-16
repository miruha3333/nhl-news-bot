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
GEMINI_FALLBACK_MODEL = "gemini-3.1-flash-lite"

GEMINI_TIMEOUT = 90
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
# DATABASE
# =========================================================

def table_exists(conn, table):
    cursor = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (table,),
    )

    return cursor.fetchone() is not None


def get_existing_columns(conn, table):
    if not table_exists(
        conn,
        table,
    ):
        return set()

    cursor = conn.execute(
        f"PRAGMA table_info({table})"
    )

    return {
        row[1]
        for row in cursor.fetchall()
    }


def add_column_if_missing(
    conn,
    table,
    column,
    definition,
):
    columns = get_existing_columns(
        conn,
        table,
    )

    if column not in columns:
        conn.execute(
            f"ALTER TABLE {table} "
            f"ADD COLUMN {column} {definition}"
        )


def remove_duplicate_news(conn):
    if not table_exists(
        conn,
        "news",
    ):
        return

    conn.execute(
        """
        DELETE FROM news
        WHERE rowid NOT IN (
            SELECT MIN(rowid)
            FROM news
            GROUP BY url
        )
        """
    )


def remove_duplicate_images(conn):
    if not table_exists(
        conn,
        "images",
    ):
        return

    conn.execute(
        """
        DELETE FROM images
        WHERE rowid NOT IN (
            SELECT MIN(rowid)
            FROM images
            GROUP BY url
        )
        """
    )


def normalize_existing_urls(
    conn,
    table,
):
    if not table_exists(
        conn,
        table,
    ):
        return

    cursor = conn.execute(
        f"SELECT rowid, url FROM {table}"
    )

    rows = cursor.fetchall()

    for rowid, url in rows:
        normalized = normalize_url(
            url
        )

        if normalized != url:
            conn.execute(
                f"""
                UPDATE {table}
                SET url = ?
                WHERE rowid = ?
                """,
                (
                    normalized,
                    rowid,
                ),
            )


def migrate_database(conn):
    print(
        "[DATABASE] Checking database schema..."
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            title TEXT,
            summary TEXT,
            published_at TEXT,
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
            source TEXT,
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
        "summary",
        "TEXT",
    )

    add_column_if_missing(
        conn,
        "news",
        "published_at",
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
        "source",
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

    normalize_existing_urls(
        conn,
        "news",
    )

    normalize_existing_urls(
        conn,
        "images",
    )

    remove_duplicate_news(
        conn
    )

    remove_duplicate_images(
        conn
    )

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
        idx_news_url_unique
        ON news(url)
        """
    )

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
        idx_errors_url
        ON errors(url)
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
        migrate_database(
            conn
        )
        DATABASE_READY = True

    return conn


def save_news(item):
    conn = get_db()

    url = normalize_url(
        item.get(
            "url",
            "",
        )
    )

    now = datetime.now(
        timezone.utc
    ).isoformat()

    conn.execute(
        """
        INSERT INTO news (
            url,
            title,
            summary,
            published_at,
            processed,
            created_at
        )
        VALUES (?, ?, ?, ?, 0, ?)
        ON CONFLICT(url) DO UPDATE SET
            title = excluded.title,
            summary = excluded.summary,
            published_at = excluded.published_at
        """,
        (
            url,
            item.get(
                "title",
                "",
            ),
            item.get(
                "summary",
                "",
            ),
            item.get(
                "published_at",
                "",
            ),
            now,
        ),
    )

    conn.commit()
    conn.close()


def is_processed(url):
    normalized = normalize_url(
        url
    )

    conn = get_db()

    cursor = conn.execute(
        """
        SELECT processed
        FROM news
        WHERE url = ?
        LIMIT 1
        """,
        (
            normalized,
        ),
    )

    row = cursor.fetchone()

    conn.close()

    return bool(
        row
        and row[0]
    )


def mark_processed(url):
    normalized = normalize_url(
        url
    )

    conn = get_db()

    conn.execute(
        """
        UPDATE news
        SET processed = 1
        WHERE url = ?
        """,
        (
            normalized,
        ),
    )

    conn.commit()
    conn.close()


def save_image(
    url,
    used=0,
):
    if not url:
        return

    conn = get_db()

    normalized = normalize_url(
        url
    )

    conn.execute(
        """
        INSERT INTO images (
            url,
            source,
            used,
            created_at
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            used = excluded.used
        """,
        (
            normalized,
            "article",
            used,
            datetime.now(
                timezone.utc
            ).isoformat(),
        ),
    )

    conn.commit()
    conn.close()


def log_error(
    url,
    error,
    stage,
):
    conn = get_db()

    conn.execute(
        """
        INSERT INTO errors (
            url,
            error,
            stage,
            created_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            normalize_url(
                url
            ),
            str(error),
            stage,
            datetime.now(
                timezone.utc
            ).isoformat(),
        ),
    )

    conn.commit()
    conn.close()


# =========================================================
# HEAVY
# =========================================================

def is_heavy_article_url(url):
    try:
        parsed = urlparse(
            url
        )

        host = parsed.netloc.lower().replace(
            "www.",
            "",
        )

        path = parsed.path.rstrip(
            "/"
        )

        if host != "heavy.com":
            return False

        if not path.startswith(
            "/sports/nhl/"
        ):
            return False

        if path == "/sports/nhl":
            return False

        return True

    except Exception:
        return False


def extract_listing_title(anchor):
    title = (
        anchor.get_text(
            " ",
            strip=True,
        )
        or anchor.get(
            "aria-label",
            "",
        ).strip()
        or anchor.get(
            "title",
            "",
        ).strip()
    )

    return re.sub(
        r"\s+",
        " ",
        title,
    ).strip()


def load_news():
    print(
        f"[HEAVY] Loading source page: "
        f"{SOURCE_URL}"
    )

    response = requests.get(
        SOURCE_URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0.0.0 "
                "Safari/537.36"
            )
        },
        timeout=30,
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    items = []
    seen = set()

    for anchor in soup.find_all(
        "a",
        href=True,
    ):
        raw_url = anchor.get(
            "href",
            "",
        ).strip()

        url = normalize_url(
            urljoin(
                SOURCE_URL,
                raw_url,
            )
        )

        if not is_heavy_article_url(
            url
        ):
            continue

        if url in seen:
            continue

        title = extract_listing_title(
            anchor
        )

        if not title:
            continue

        seen.add(
            url
        )

        items.append(
            {
                "title": title,
                "url": url,
                "summary": "",
                "published_at": "",
            }
        )

        if len(items) >= MAX_NEWS:
            break

    print(
        f"[HEAVY] Articles found: "
        f"{len(items)}"
    )

    for index, item in enumerate(
        items,
        1,
    ):
        print(
            f"[HEAVY] {index}. "
            f"{item['title']} | "
            f"{item['url']}"
        )

    return items


# =========================================================
# ARTICLE IMAGES
# =========================================================

def normalize_image_url(
    image_url,
    page_url,
):
    if not image_url:
        return ""

    image_url = image_url.strip()

    if image_url.startswith(
        "//"
    ):
        image_url = (
            "https:"
            + image_url
        )

    image_url = urljoin(
        page_url,
        image_url,
    )

    return image_url


def extract_jsonld_images(
    value,
    page_url,
):
    results = []

    if isinstance(
        value,
        str,
    ):
        normalized = normalize_image_url(
            value,
            page_url,
        )

        if normalized:
            results.append(
                normalized
            )

        return results

    if isinstance(
        value,
        list,
    ):
        for item in value:
            results.extend(
                extract_jsonld_images(
                    item,
                    page_url,
                )
            )

        return results

    if isinstance(
        value,
        dict,
    ):
        image = value.get(
            "image"
        )

        if image:
            results.extend(
                extract_jsonld_images(
                    image,
                    page_url,
                )
            )

        return results

    return results


def extract_source_images(
    soup,
    page_url,
):
    candidates = []

    meta_selectors = [
        (
            "meta",
            {
                "property": "og:image"
            },
        ),
        (
            "meta",
            {
                "property": "og:image:url"
            },
        ),
        (
            "meta",
            {
                "property": "og:image:secure_url"
            },
        ),
        (
            "meta",
            {
                "name": "twitter:image"
            },
        ),
        (
            "meta",
            {
                "name": "twitter:image:src"
            },
        ),
    ]

    for tag_name, attrs in meta_selectors:
        tag = soup.find(
            tag_name,
            attrs=attrs,
        )

        if not tag:
            continue

        value = (
            tag.get(
                "content",
                "",
            )
            or ""
        ).strip()

        normalized = normalize_image_url(
            value,
            page_url,
        )

        if normalized:
            candidates.append(
                (
                    normalized,
                    "article_meta",
                )
            )

    link_tag = soup.find(
        "link",
        rel=lambda value: (
            value
            and "image_src" in value
        ),
    )

    if link_tag:
        normalized = normalize_image_url(
            link_tag.get(
                "href",
                "",
            ),
            page_url,
        )

        if normalized:
            candidates.append(
                (
                    normalized,
                    "article_link",
                )
            )

    for script in soup.find_all(
        "script",
        type="application/ld+json",
    ):
        raw = script.string

        if not raw:
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
                    "article_jsonld",
                )
            )

    result = []
    seen = set()

    for image_url, source in candidates:
        if image_url in seen:
            continue

        seen.add(
            image_url
        )

        result.append(
            (
                image_url,
                source,
            )
        )

    return result


def fetch_article(
    url,
    fallback_summary="",
):
    print(
        f"[ARTICLE] Loading: "
        f"{url}"
    )

    response = requests.get(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0.0.0 "
                "Safari/537.36"
            )
        },
        timeout=30,
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    source_images = extract_source_images(
        soup,
        url,
    )

    print(
        f"[IMAGE] Article source image candidates: "
        f"{len(source_images)}"
    )

    article_node = soup.find(
        "article"
    )

    if article_node is None:
        article_node = soup.find(
            "main"
        )

    if article_node is None:
        article_node = soup

    for unwanted in article_node.find_all(
        [
            "script",
            "style",
            "noscript",
            "svg",
            "nav",
            "footer",
            "header",
        ]
    ):
        unwanted.decompose()

    article_text = article_node.get_text(
        " ",
        strip=True,
    )

    article_text = re.sub(
        r"\s+",
        " ",
        article_text,
    ).strip()

    if len(article_text) < 300:
        fallback = re.sub(
            r"\s+",
            " ",
            fallback_summary or "",
        ).strip()

        if fallback:
            article_text = (
                article_text
                + " "
                + fallback
            ).strip()

    if len(article_text) < 300:
        raise RuntimeError(
            "Article text is too short"
        )

    return (
        article_text[:MAX_ARTICLE_TEXT],
        source_images,
    )


def download_source_image(
    image_url,
    page_url,
    image_source,
):
    if not image_url:
        return None

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(X11; Linux x86_64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/140.0.0.0 "
            "Safari/537.36"
        ),
        "Referer": page_url,
    }

    try:
        response = requests.get(
            image_url,
            headers=headers,
            timeout=SOURCE_IMAGE_DOWNLOAD_TIMEOUT,
        )

        response.raise_for_status()

        content_type = (
            response.headers.get(
                "Content-Type",
                "",
            )
            .lower()
            .strip()
        )

        if not content_type.startswith(
            "image/"
        ):
            print(
                "[IMAGE] Skipping non-image response: "
                f"{image_url}"
            )
            return None

        content = response.content

        if len(content) < MIN_IMAGE_BYTES:
            print(
                "[IMAGE] Image is too small: "
                f"{len(content)} bytes"
            )
            return None

        extension = ".jpg"

        if "png" in content_type:
            extension = ".png"
        elif "webp" in content_type:
            extension = ".webp"
        elif "gif" in content_type:
            extension = ".gif"

        filename = (
            "nhl_"
            + hashlib.sha256(
                image_url.encode(
                    "utf-8"
                )
            ).hexdigest()[:16]
            + extension
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
                content
            )

        save_image(
            image_url,
            used=0,
        )

        print(
            "[IMAGE] Downloaded source image: "
            f"{image_source}"
        )

        return path

    except Exception as exc:
        print(
            "[IMAGE ERROR] "
            f"{image_url}: "
            f"{exc}"
        )

        return None


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
            "maxOutputTokens": 500,
        },
    }

    response = requests.post(
        url,
        params={
            "key": GEMINI_API_KEY
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
        .get("content", {})
        .get("parts", [])
    )

    text = "".join(
        part.get("text", "")
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


def validate_post_structure(text):
    """Deterministic checks for the Telegram post format."""
    text = clean_post(
        text
    )

    length = len(
        text
    )

    if length < 400 or length > 600:
        raise PostRejected(
            f"Post length is {length} characters; required 400-600"
        )

    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(
            r"\n\s*\n",
            text,
        )
        if paragraph.strip()
    ]

    if len(paragraphs) != 3:
        raise PostRejected(
            "Post must contain exactly 3 paragraphs; "
            f"found {len(paragraphs)}"
        )

    paragraph_lengths = [
        len(paragraph)
        for paragraph in paragraphs
    ]

    if min(
        paragraph_lengths
    ) < 80:
        raise PostRejected(
            "One of the paragraphs is too short"
        )

    if (
        max(paragraph_lengths)
        > min(paragraph_lengths) * 1.8
    ):
        raise PostRejected(
            "Paragraphs are not approximately equal in length"
        )

    if (
        "«" in text
        or "»" in text
    ):
        raise PostRejected(
            "Post contains Russian quotation marks"
        )

    if (
        text.startswith("-")
        or text.startswith("•")
    ):
        raise PostRejected(
            "Post looks like a list instead of a normal Telegram post"
        )

    return text


def generate_post_prompt(
    title,
    article,
):
    return f"""
Ты пишешь короткий пост для русскоязычного Telegram-канала про NHL.

Твоя задача — сделать точную и грамотную выжимку самого важного из материала.

ЖЁСТКИЕ ТРЕБОВАНИЯ:

1. Итоговый текст должен содержать от 400 до 600 символов включительно. Целься в 460-520 символов, чтобы не выйти за предел. Считай все символы, включая пробелы и знаки препинания.
2. Сделай ровно 3 органичных абзаца.
3. Каждый абзац должен быть примерно 140-180 символов. Не делай один абзац заметно длиннее остальных.
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

    def repair_prompt(
        previous_post,
    ):
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

    def generate_with_model(
        model,
        label,
        request_prompt,
    ):
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
                        clean_post(
                            result
                        )
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
                    clean_post(
                        result
                    )
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


def check_post_language(
    post,
):
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

Если ошибок нет, ответь строго:

OK

Если есть хотя бы одна реальная ошибка, ответь строго:

ERROR

Никаких пояснений.

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

            error_text = str(
                exc
            ).lower()

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
                "Language validation service failed: "
                f"{exc}"
            ) from exc

    answer = clean_post(
        result
    ).upper()

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
        save_news(
            item
        )

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
            check_post_language(
                post
            )

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

            mark_processed(
                url
            )

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

        save_image(
            source_images[0][0]
            if source_images
            else "",
            used=1,
        )

        mark_processed(
            url
        )

        print(
            "[POST PUBLISHED] "
            f"{item['title']}"
        )

        return True

    except PostRejected as exc:
        print(
            "[POST SKIPPED] "
            f"{exc}"
        )

        log_error(
            url,
            str(exc),
            "post_generation",
        )

        mark_processed(
            url
        )

        return False

    except Exception as exc:
        print(
            "[BOT ERROR] "
            f"{exc}"
        )

        log_error(
            url,
            str(exc),
            "processing",
        )

        return False


# =========================================================
# MAIN
# =========================================================

def main():
    print(
        "=" * 70
    )

    print(
        "NHL NEWS BOT START"
    )

    print(
        "=" * 70
    )

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
        "[CONFIG] Post length: "
        "400-600 characters; 3 paragraphs"
    )

    print(
        "=" * 70
    )

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

    print(
        "=" * 70
    )

    print(
        "NHL NEWS BOT FINISHED"
    )

    print(
        f"Published: {published}"
    )

    print(
        f"Failed: {failed}"
    )

    print(
        "=" * 70
    )


if __name__ == "__main__":
    main()
