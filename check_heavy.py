import os
import sqlite3
import sys
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


SOURCE_URL = "https://heavy.com/sports/nhl/"
DATABASE_FILE = "nhl_bot.db"
MAX_NEWS = 30


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
# HEAVY ARTICLE URL
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


def extract_title(anchor):
    title = (
        anchor.get_text(" ", strip=True)
        or anchor.get("aria-label", "")
        or anchor.get("title", "")
        or ""
    )

    return " ".join(title.split()).strip()


# =========================================================
# HEAVY LISTING
# =========================================================

def load_heavy_articles():
    print(
        f"[WATCHER] Loading Heavy: {SOURCE_URL}"
    )

    response = requests.get(
        SOURCE_URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36 "
                "NHLNewsWatcher/1.0"
            )
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

        title = extract_title(anchor)

        if not title:
            continue

        seen.add(url)

        result.append(
            {
                "url": url,
                "title": title,
            }
        )

        if len(result) >= MAX_NEWS:
            break

    return result


# =========================================================
# DATABASE
# =========================================================

def get_processed_urls():
    if not os.path.exists(
        DATABASE_FILE
    ):
        print(
            "[WATCHER] Database file does not exist yet; "
            "all Heavy articles are considered new."
        )

        return set()

    try:
        conn = sqlite3.connect(
            DATABASE_FILE
        )

        table = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            AND name = 'news'
            """
        ).fetchone()

        if not table:
            conn.close()

            print(
                "[WATCHER] news table does not exist yet; "
                "all Heavy articles are considered new."
            )

            return set()

        columns = {
            row[1]
            for row in conn.execute(
                'PRAGMA table_info("news")'
            ).fetchall()
        }

        if "url" not in columns:
            conn.close()

            print(
                "[WATCHER] news table has no url column; "
                "all Heavy articles are considered new."
            )

            return set()

        if "processed" not in columns:
            conn.close()

            print(
                "[WATCHER] news table has no processed column; "
                "all Heavy articles are considered new."
            )

            return set()

        rows = conn.execute(
            """
            SELECT url
            FROM news
            WHERE processed = 1
            """
        ).fetchall()

        conn.close()

        return {
            normalize_url(row[0])
            for row in rows
            if row[0]
        }

    except sqlite3.Error as exc:
        print(
            f"[WATCHER ERROR] SQLite read failed: {exc}"
        )

        raise


# =========================================================
# GITHUB OUTPUT
# =========================================================

def set_github_output(
    key,
    value,
):
    output_file = os.getenv(
        "GITHUB_OUTPUT"
    )

    if not output_file:
        return

    with open(
        output_file,
        "a",
        encoding="utf-8",
    ) as file:
        file.write(
            f"{key}={value}\n"
        )


# =========================================================
# MAIN
# =========================================================

def main():
    print("=" * 70)
    print("NHL HEAVY WATCHER START")
    print("=" * 70)

    try:
        articles = load_heavy_articles()

    except Exception as exc:
        print(
            f"[WATCHER ERROR] Heavy loading failed: {exc}"
        )

        set_github_output(
            "NEW_NEWS",
            "false",
        )

        sys.exit(1)

    print(
        f"[WATCHER] Heavy articles found: "
        f"{len(articles)}"
    )

    if not articles:
        print(
            "[WATCHER ERROR] Heavy returned no NHL articles."
        )

        set_github_output(
            "NEW_NEWS",
            "false",
        )

        sys.exit(1)

    processed_urls = get_processed_urls()

    new_articles = [
        article
        for article in articles
        if normalize_url(
            article["url"]
        ) not in processed_urls
    ]

    print(
        "[WATCHER] Already processed: "
        f"{len(articles) - len(new_articles)}"
    )

    print(
        "[WATCHER] New articles: "
        f"{len(new_articles)}"
    )

    for index, article in enumerate(
        new_articles,
        1,
    ):
        print(
            f"[WATCHER] NEW {index}. "
            f"{article['title']} | "
            f"{article['url']}"
        )

    if new_articles:
        set_github_output(
            "NEW_NEWS",
            "true",
        )

        set_github_output(
            "NEWS_COUNT",
            str(len(new_articles)),
        )

        set_github_output(
            "NEWS_TITLE",
            new_articles[0]["title"]
            .replace("\n", " ")
            .replace("\r", " "),
        )

        set_github_output(
            "NEWS_LINK",
            new_articles[0]["url"],
        )

        print(
            "[WATCHER] New Heavy articles detected."
        )

    else:
        set_github_output(
            "NEW_NEWS",
            "false",
        )

        set_github_output(
            "NEWS_COUNT",
            "0",
        )

        print(
            "[WATCHER] No new Heavy articles. "
            "Nothing to trigger."
        )

    print(
        "[WATCHER] Watcher finished successfully"
    )

    print("=" * 70)


if __name__ == "__main__":
    main()
