import g4f
import feedparser
import telebot
import os
import time
import requests
from duckduckgo_search import DDGS

TOKEN = os.environ.get('TOKEN') 
CHANNEL_ID = '-1004423088204' # Твой ID канала

bot = telebot.TeleBot(TOKEN)
HISTORY_FILE = "history.txt"

def get_history():
    if not os.path.exists(HISTORY_FILE): 
        return set()
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f)

def add_to_history(title):
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(title + "\n")

def download_image(query):
    """Ищет картинки и пробует скачать несколько вариантов для надежности"""
    print(f"Ищем картинку по запросу: {query}")
    try:
        with DDGS() as ddgs:
            # Запрашиваем 3 картинки на случай, если первая битая
            results = list(ddgs.images(keywords=query, max_results=3))
            
            for res in results:
                try:
                    img_url = res['image']
                    # Пытаемся скачать (с таймаутом, чтобы не зависнуть)
                    img_data = requests.get(img_url, timeout=10).content
                    img_name = "temp.jpg"
                    with open(img_name, 'wb') as handler:
                        handler.write(img_data)
                    return img_name # Успех! Картинка скачана
                except Exception as e:
                    print(f"Не удалось скачать {img_url}: {e}")
                    continue # Пробуем следующую ссылку из топ-3
    except Exception as e:
        print(f"Ошибка DuckDuckGo: {e}")
        
    return None # Если ничего не вышло

def main():
    feed = feedparser.parse("https://nitter.net/NHLRumourReport/rss")
    history = get_history()
    
    for entry in reversed(feed.entries[:5]):
        if entry.title not in history:
            raw_response = translate_tweet(entry.title)
            
            if raw_response:
                if "SEARCH_QUERY:" in raw_response:
                    parts = raw_response.split("SEARCH_QUERY:")
                    post_text = parts[0].strip()
                    search_query = parts[1].strip()
                else:
                    post_text = raw_response
                    search_query = None
                
                image_path = None
                
                # План А: Ищем специфичную картинку по игроку/команде
                if search_query:
                    image_path = download_image(search_query)
                
                # План Б: Если нейросеть не дала запрос или картинка не скачалась, 
                # ищем универсальную красивую хоккейную картинку
                if not image_path:
                    print("План Б: ищем дефолтную картинку...")
                    image_path = download_image("NHL ice hockey game action photography")
                
                try:
                    # План В: На всякий случай проверяем, точно ли файл есть на диске
                    if image_path and os.path.exists(image_path):
                        with open(image_path, 'rb') as photo:
                            bot.send_photo(CHANNEL_ID, photo, caption=post_text)
                        os.remove(image_path)
                        print("✅ Пост с картинкой отправлен!")
                    else:
                        bot.send_message(CHANNEL_ID, post_text)
                        print("⚠️ Пост отправлен БЕЗ картинки (все попытки провалились).")
                        
                    add_to_history(entry.title)
                except Exception as e:
                    print(f"❌ Ошибка отправки в Telegram: {e}")
                    
                # Увеличиваем паузу, чтобы DuckDuckGo и Telegram не блокировали за спам
                time.sleep(5)
    return None

def main():
    feed = feedparser.parse("https://nitter.net/NHLRumourReport/rss")
    history = get_history()
    
    for entry in reversed(feed.entries[:5]):
        if entry.title not in history:
            raw_response = translate_tweet(entry.title)
            
            if raw_response:
                if "SEARCH_QUERY:" in raw_response:
                    parts = raw_response.split("SEARCH_QUERY:")
                    post_text = parts[0].strip()
                    search_query = parts[1].strip()
                else:
                    post_text = raw_response
                    search_query = None
                
                image_path = None
                if search_query:
                    print(f"Ищем картинку по запросу: {search_query}")
                    image_path = download_image(search_query)
                
                try:
                    if image_path and os.path.exists(image_path):
                        with open(image_path, 'rb') as photo:
                            bot.send_photo(CHANNEL_ID, photo, caption=post_text)
                        os.remove(image_path)
                    else:
                        bot.send_message(CHANNEL_ID, post_text)
                        
                    add_to_history(entry.title)
                except Exception as e:
                    print(f"Ошибка отправки: {e}")
                    
                time.sleep(2)

if __name__ == "__main__":
    main()
