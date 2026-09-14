import feedparser
import os
import sys
import requests

HISTORY_FILE = "history.txt"

def get_history():
    if not os.path.exists(HISTORY_FILE): 
        return set()
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f)

def main():
    # Используем рабочее зеркало Nitter
    rss_url = "https://nitter.poast.org/NHLRumourReport/rss"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
    
    try:
        # Загружаем RSS через requests, чтобы отловить ошибки HTTP
        response = requests.get(rss_url, headers=headers, timeout=15)
        response.raise_for_status()
        
        feed = feedparser.parse(response.content)
        
        if not feed.entries:
            print("Ошибка: RSS лента пуста или недоступна.")
            sys.exit(1)
            
        last_entry = feed.entries[0]
        last_title = last_entry.title.strip()
        
        print(f"Последняя новость на сайте: {last_title}")
        
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
                    
    except Exception as e:
        print(f"Произошла ошибка при разборе RSS: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
