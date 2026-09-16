import os
import re
import hashlib
import json
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


# =========================================================
# CONFIG
# =========================================================

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
ARTICLE_TIMEOUT = 30
IMAGE_TIMEOUT = 20
MIN_IMAGE_BYTES = 5000
GEMINI_DELAY = 1.0
MAX_ARTICLE_TEXT = 12000

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
# DATABASE
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


def add_column_if_missing(conn, table, column, definition):
    if column not in get_existing_columns(conn, table):
        conn.execute(
            f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}'
        )


def normalize_existing_urls(conn, table):
    if not table_exists(conn, table):
        return

    if "url" not in get_existing_columns(conn, table):
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


def remove_duplicate_rows(conn, table):
    if not table_exists(conn, table):
        return

    rows = conn.execute(
        f'SELECT rowid, url FROM "{table}" ORDER BY rowid'
    ).fetchall()

    seen = set()

    for rowid, url in rows:
        normalized = normalize_url(url)

        if not normalized:
            continue

        if normalized in seen:
            conn.execute(
                f'DELETE FROM "{table}" WHERE rowid = ?',
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
            processed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    for column, definition in (
        ("title", "TEXT"),
        ("source", "TEXT"),
        ("published", "TEXT"),
        ("processed", "INTEGER NOT NULL DEFAULT 0"),
        ("created_at", "TEXT"),
    ):
        add_column_if_missing(conn, "news", column, definition)

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

    for column, definition in (
        ("used", "INTEGER NOT NULL DEFAULT 0"),
        ("created_at", "TEXT"),
    ):
        add_column_if_missing(conn, "images", column, definition)

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

    for column, definition in (
        ("url", "TEXT"),
        ("error", "TEXT"),
        ("error_message", "TEXT"),
        ("stage", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("created_at", "TEXT"),
    ):
        add_column_if_missing(conn, "errors", column, definition)

    normalize_existing_urls(conn, "news")
    normalize_existing_urls(conn, "images")
    remove_duplicate_rows(conn, "news")
    remove_duplicate_rows(conn, "images")

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_news_url_unique
        ON news(url)
        """
    )

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_images_url_unique
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

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    if not DATABASE_READY:
        migrate_database(conn)
        DATABASE_READY = True

    return conn


def save_news(item):
    conn = get_db()

    url = normalize_url(item.get("url", ""))

    row = conn.execute(
        "SELECT id FROM news WHERE url = ?",
        (url,),
    ).fetchone()

    if row:
        news_id = row[0]

        conn.execute(
            """
            UPDATE news
            SET title = ?, source = ?, published = ?
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
        cursor = conn.execute(
            """
            INSERT INTO news (
                url, title, source, published, processed, created_at
            )
            VALUES (?, ?, ?, ?, 0, ?)
            """,
            (
                url,
                item.get("title", ""),
                item.get("source", ""),
                item.get("published", ""),
                datetime.now(timezone.utc).isoformat(),
            ),
        )

        news_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return news_id


def is_processed(url):
    conn = get_db()

    row = conn.execute(
        "SELECT processed FROM news WHERE url = ?",
        (normalize_url(url),),
    ).fetchone()

    conn.close()

    return bool(row and row[0])


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


def save_image(url, used=0):
    url = normalize_url(url)

    if not url:
        return

    conn = get_db()

    row = conn.execute(
        "SELECT id FROM images WHERE url = ?",
        (url,),
    ).fetchone()

    if row:
        conn.execute(
            "UPDATE images SET used = ? WHERE id = ?",
            (used, row[0]),
        )
    else:
        conn.execute(
            """
            INSERT INTO images (url, used, created_at)
            VALUES (?, ?, ?)
            """,
            (
                url,
                used,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    conn.commit()
    conn.close()


def log_error(url, error, stage="unknown"):
    message = str(error)[:4000]

    try:
        conn = get_db()
        columns = get_existing_columns(conn, "errors")

        fields = []
        values = []

        if "url" in columns:
            fields.append("url")
            values.append(url)

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
            values.append(datetime.now(timezone.utc).isoformat())

        placeholders = ",".join("?" for _ in fields)

        conn.execute(
            f"""
            INSERT INTO errors ({",".join(fields)})
            VALUES ({placeholders})
            """,
            tuple(values),
        )

        conn.commit()
        conn.close()

    except Exception as exc:
        print(f"[ERROR LOGGER FAILURE] {exc}")


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
        f"[HEAVY] Loading source page: "
        f"{SOURCE_URL}"
    )

    response = requests.get(
        SOURCE_URL,
        headers={
            "User-Agent":
                "Mozilla/5.0 "
                "(X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36 "
                "NHLNewsBot/1.0"
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

        if not is_heavy_article_url(url):
            continue

        if url in seen:
            continue

        title = extract_listing_title(
            anchor
        )

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
        f"[HEAVY] Articles found: "
        f"{len(result)}"
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

        seen.add(image_url)

        unique.append(
            (
                image_url,
                source,
            )
        )

    return unique


def fetch_article(url):
    print(
        f"[ARTICLE] Loading: "
        f"{url}"
    )

    response = requests.get(
        url,
        headers={
            "User-Agent":
                "Mozilla/5.0 "
                "(X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36 "
                "NHLNewsBot/1.0"
        },
        timeout=30,
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    final_url = (
        response.url
        or url
    )

    source_images = (
        extract_source_images(
            soup,
            final_url,
        )
    )

    print(
        "[IMAGE] Article source image "
        "candidates: "
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
            "nav",
            "footer",
            "form",
        ]
    ):
        tag.decompose()

    article_node = (
        text_soup.find(
            "article"
        )
        or text_soup.find(
            "main"
        )
    )

    if article_node:
        text = article_node.get_text(
            " ",
            strip=True,
        )
    else:
        text = text_soup.get_text(
            " ",
            strip=True,
        )

    text = re.sub(
        r"\s+",
        " ",
        text,
    ).strip()

    if len(text) < 300:
        raise RuntimeError(
            "Article text is too short"
        )

    return (
        text[:MAX_ARTICLE_TEXT],
        source_images,
    )


# =========================================================
# SOURCE IMAGE
# =========================================================

def download_source_image(
    image_url,
    article_url,
    image_source,
):
    if not image_url:
        return None

    print(
        "[IMAGE] Trying article image: "
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
            timeout=20,
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
                "[IMAGE] URL is not an image: "
                f"{content_type or 'unknown'}"
            )

            return None

        if len(response.content) < MIN_IMAGE_BYTES:
            print(
                "[IMAGE] Image is too small: "
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
            "[IMAGE] Downloaded from article: "
            f"{path}"
        )

        return path

    except Exception as exc:
        print(
            "[IMAGE SOURCE ERROR] "
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

    response = requests.post(
        url,
        params={
            "key":
                GEMINI_API_KEY
        },
        json={
            "contents": [
                {
                    "parts": [
                        {
                            "text":
                                prompt
                        }
                    ]
                }
            ]
        },
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
                    "[GEMINI] Primary disabled "
                    "for the remainder of this run; "
                    "using fallback model."
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

    result = gemini_request(
        GEMINI_FALLBACK_MODEL,
        prompt,
    )

    time.sleep(
        GEMINI_DELAY
    )

    return clean_post(
        result
    )


# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(
    post,
    image_path,
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

    if not image_path:
        raise RuntimeError(
            "No article image available"
        )

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
            timeout=60,
        )

    if response.status_code >= 400:
        raise RuntimeError(
            "Telegram HTTP "
            f"{response.status_code}: "
            f"{response.text[:1000]}"
        )


# =========================================================
# PROCESS
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
            url
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
            raise RuntimeError(
                "Could not download any image "
                "from the article page"
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

        elif "image" in message.lower():
            stage = "image"

        elif (
            "article" in message.lower()
            or "HTTP" in message
        ):
            stage = "article"

        else:
            stage = "processing"

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
        f"[CONFIG] Donor: "
        f"{SOURCE_URL}"
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

    print("=" * 70)

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
        f"Published: "
        f"{published}"
    )

    print(
        f"Failed: "
        f"{failed}"
    )

    print("=" * 70)


if __name__ == "__main__":
    main()
