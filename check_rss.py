import os
import sys
import urllib.request
import feedparser

HISTORY_FILE = "history.txt"
RSS_URL = "https://rss.app/feeds/sbl4f7OUFIrh9Wsk.xml"


def get_history():
    if not os.path.exists(HISTORY_FILE):
        return set()
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def append_history(item_id):
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(f"{item_id}\n")


def set_github_output(key, value):
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def main():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }

    try:
        print(f"Подключение к RSS: {RSS_URL}...")
        req = urllib.request.Request(RSS_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as response:
            content = response.read()
            feed = feedparser.parse(content)
    except Exception as e:
        print(f"Ошибка при загрузке RSS: {e}")
        sys.exit(1)

    if not feed or not feed.entries:
        print("Ошибка: Поток пуст или не содержит записей.")
        sys.exit(1)

    last_entry = feed.entries[0]

    # Идентификатор поста (обычно ссылка или guid — надежнее, чем просто заголовок)
    entry_id = getattr(last_entry, "id", None) or getattr(last_entry, "link", None)

    # Получаем заголовок или текст записи
    last_title = getattr(last_entry, "title", "").strip()
    if not last_title and hasattr(last_entry, "summary"):
        last_title = last_entry.summary.strip()

    # Для сверки с историей используем id (если его нет — заголовок)
    check_item = entry_id if entry_id else last_title

    print(f"Последняя новость: {last_title}")

    history = get_history()

    if check_item not in history:
        print("Найдена новая новость!")
        # Добавляем в историю, чтобы при следующем запуске новость не считалась новой
        append_history(check_item)

        # Передаем переменные в GitHub Actions
        set_github_output("NEW_NEWS", "true")
        
        # Очищаем переносы строк для безопасной передачи одной строкой в Actions
        clean_title = last_title.replace("\n", " ").replace("\r", " ")
        set_github_output("NEWS_TITLE", clean_title)
        
        entry_link = getattr(last_entry, "link", "")
        set_github_output("NEWS_LINK", entry_link)
    else:
        print("Новых новостей нет. Спим дальше.")
        set_github_output("NEW_NEWS", "false")


if __name__ == "__main__":
    main()
