import feedparser
import os
import sys
import urllib.request

HISTORY_FILE = "history.txt"

# Публичные узлы RSS-Bridge для парсинга аккаунта @NHLRumourReport
RSS_URLS = [
    "https://rss-bridge.org/bridge01/?action=display&bridge=TwitterBridge&context=By+username&u=NHLRumourReport&format=Atom",
    "https://bridge.nodal.zone/?action=display&bridge=TwitterBridge&context=By+username&u=NHLRumourReport&format=Atom",
    "https://rss.dresden.network/?action=display&bridge=TwitterBridge&context=By+username&u=NHLRumourReport&format=Atom",
    "https://rssbridge.pw/?action=display&bridge=TwitterBridge&context=By+username&u=NHLRumourReport&format=Atom"
]

def get_history():
    if not os.path.exists(HISTORY_FILE): 
        return set()
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f)

def main():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
    
    feed = None
    last_error = None

    for url in RSS_URLS:
        try:
            print(f"Подключение к RSS-мосту: {url[:50]}...")
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=12) as response:
                content = response.read()
                parsed = feedparser.parse(content)
                if parsed.entries:
                    feed = parsed
                    print("Успешно получены данные!")
                    break
        except Exception as e:
            print(f"Сбой моста, пробуем следующий... ({e})")
            last_error = e

    if not feed or not feed.entries:
        print(f"Ошибка: Все RSS-мосты недоступны. Последняя ошибка: {last_error}")
        sys.exit(1)
        
    last_entry = feed.entries[0]
    
    # Получаем заголовок или текст записи
    last_title = getattr(last_entry, 'title', '').strip()
    if not last_title and hasattr(last_entry, 'summary'):
        last_title = last_entry.summary.strip()
    
    print(f"Последняя новость: {last_title}")
    
    history = get_history()
    
    if last_title not in history:
        print("Найдена новая новость!")
        if "GITHUB_OUTPUT" in os.environ:
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write("NEW_NEWS=true\n")
    else:
        print("Новых новостей нет. Спим дальше.")
        if "GITHUB_OUTPUT" in os.environ:
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write("NEW_NEWS=false\n")

if __name__ == "__main__":
    main()
