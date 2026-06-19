import g4f
import feedparser
import time

HISTORY_FILE = "history.txt"

def get_history():
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f)
    except FileNotFoundError:
        return set()

def add_to_history(title):
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(title + "\n")

def translate_tweet_with_ai(text):
    # Добавили правило №4: удалять всё, что идет после последнего тире
    prompt = f"""
    Ты — редактор хоккейного канала. Переведи твит на живой русский язык.
    ПРАВИЛА:
    1. Имена и фамилии оставляй в ОРИГИНАЛЕ (Darnell Nurse, Drew Doughty).
    2. Термины: Cap hit -> кэпхит, Trade -> обмен, Free agent -> свободный агент.
    3. Выдай ТОЛЬКО перевод.
    4. ВАЖНО: Удали из текста любую информацию об источнике (например, "- The Athletic (6/16)" или "- Oilers Now (6/15)"). 
       Оставляй только суть новости.
    Текст: "{text}"
    """
    for attempt in range(3):
        try:
            return g4f.ChatCompletion.create(model=g4f.models.gpt_4o, messages=[{"role": "user", "content": prompt}])
        except:
            time.sleep(2)
    return None

def main():
    rss_url = "https://nitter.net/NHLRumourReport/rss"
    feed = feedparser.parse(rss_url)
    history = get_history()
    
    print("🚀 Проверка новых новостей...\n")
    
    for entry in reversed(feed.entries[:5]):
        if entry.title not in history:
            print(f"✨ Новая новость: {entry.title}")
            translated = translate_tweet_with_ai(entry.title)
            
            if translated:
                print(f"✅ Перевод: {translated}")
                add_to_history(entry.title)
            print("-" * 50)

if __name__ == "__main__":
    main()
