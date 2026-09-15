import os
import sys
import urllib.request
import feedparser

from database import init_db, news_exists


RSS_URL = "https://rss.app/feeds/sbl4f7OUFIrh9Wsk.xml"


def set_github_output(key, value):
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def clean_raw_rss_text(raw_text):
    if not raw_text:
        return ""

    return raw_text.strip()


def get_entry_id(entry):
    return (
        getattr(entry, "id", None)
        or getattr(entry, "guid", None)
        or getattr(entry, "link", None)
        or clean_raw_rss_text(getattr(entry, "title", ""))
    )


def main():
    init_db()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        )
    }

    try:
        print(f"Подключение к RSS: {RSS_URL}...")

        req = urllib.request.Request(
            RSS_URL,
            headers=headers,
        )

        with urllib.request.urlopen(req, timeout=15) as response:
            content = response.read()
            feed = feedparser.parse(content)

    except Exception as e:
        print(f"Ошибка при загрузке RSS: {e}")
        set_github_output("NEW_NEWS", "false")
        sys.exit(1)

    if not feed or not feed.entries:
        print("Ошибка: RSS-поток пуст или не содержит записей.")
        set_github_output("NEW_NEWS", "false")
        sys.exit(1)

    print(f"Получено записей из RSS: {len(feed.entries)}")

    new_entries = []

    for entry in feed.entries[:30]:
        entry_id = get_entry_id(entry)

        title = (
            getattr(entry, "title", "").strip()
            or getattr(entry, "summary", "").strip()
        )

        if not entry_id:
            print("⚠️ У записи отсутствует ID. Пропускаем.")
            continue

        if not title:
            print(f"⚠️ У записи {entry_id} отсутствует заголовок. Пропускаем.")
            continue

        if news_exists(entry_id):
            continue

        new_entries.append(
            {
                "id": entry_id,
                "title": title,
            }
        )

    if not new_entries:
        print("Новых новостей нет.")
        set_github_output("NEW_NEWS", "false")
        return

    print(f"Найдено новых новостей: {len(new_entries)}")

    latest = new_entries[0]

    print(f"Последняя новая новость: {latest['title']}")

    set_github_output("NEW_NEWS", "true")
    set_github_output("NEWS_TITLE", latest["title"].replace("\n", " ").replace("\r", " "))
    set_github_output("NEWS_LINK", "")


if __name__ == "__main__":
    main()
