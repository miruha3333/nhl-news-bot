import feedparser
import os
import sys

HISTORY_FILE = "history.txt"

def get_history():
    if not os.path.exists(HISTORY_FILE): 
        return set()
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f)

def main():
    try:
        feed = feedparser.parse("https://nitter.net/NHLRumourReport/rss")
        
        # Проверяем, что лента успешно загрузилась и в ней есть записи
        if not feed.entries:
            print("Ошибка: RSS лента пуста или недоступна.")
            sys.exit(1)
            
        last_entry = feed.entries[0]
        last_title = last_entry.title.strip()
        
        print(f"Последняя новость на сайте: {last_title}")
        
        history = get_history()
        
        if last_title not in history:
            print("Найдена новая новость!")
            # Записываем переменную для GitHub Actions в специальный файл среды
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
