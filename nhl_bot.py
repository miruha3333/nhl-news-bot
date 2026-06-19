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
    """Ищет картинку в интернете и скачивает её на диск GitHub"""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.images(keywords=query, max_results=1))
            if results:
                img_url = results[0]['image']
                # Скачиваем картинку
                img_data = requests.get(img_url, timeout=10).content
                img_name = "temp.jpg"
                with open(img_name, 'wb') as handler:
                    handler.write(img_data)
                return img_name
    except Exception as e:
        print(f"Не удалось загрузить картинку: {e}")
    return None

def translate_tweet(text):
    # Добавили правило №7 для генерации поискового запроса картинки
    prompt = f"""
    Ты — автоматический хоккейный редактор. Переведи твит о НХЛ на русский язык.
    
    СТРОГИЕ ПРАВИЛА ФОРМАТИРОВАНИЯ ПОСТА:
    1. Формат вывода должен быть строго: Источник (на английском языке): Текст перевода.
    2. Если в начале оригинального текста указан автор/источник (например, "Chris Johnston:"), оставь его имя на английском в самом начале поста, затем поставь двоеточие и пробел.
    3. Убери из итогового текста ВСЕ лишние знаки: кавычки, звездочки (**). Текст должен быть абсолютно чистым.
    4. Все имена и фамилии игроков внутри перевода оставляй в ОРИГИНАЛЕ на английском (Darnell Nurse, Drew Doughty).
    5. Хоккейные термины: Cap hit -> кэпхит, Trade -> обмен, Free agent -> свободный агент.
    6. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО добавлять от себя любые вводные фразы ("Вот перевод..."). 
    7. ВАЖНО: В самой последней строке ответа напиши строго: SEARCH_QUERY: [Имя главного игрока или команды из текста на английском] NHL photo.
    
    Оригинальный текст: "{text}"
    """
    
    for attempt in range(3):
        try:
            response = g4f.ChatCompletion.create(
                model=g4f.models.gpt_4o, 
                messages=[{"role": "user", "content": prompt}]
            )
            if response:
                clean_text = response.replace("**", "").replace('"', "").replace("«", "").replace("»", "")
                return clean_text.strip()
        except:
            time.sleep(2)
    return None

def main():
    feed = feedparser.parse("https://nitter.net/NHLRumourReport/rss")
    history = get_history()
    
    for entry in reversed(feed.entries[:5]):
        if entry.title not in history:
            raw_response = translate_tweet(entry.title)
            
            if raw_response:
                # Разделяем текст поста и поисковый запрос картинки
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
                
                # Отправка в Telegram
                try:
                    if image_path and os.path.exists(image_path):
                        # Если картинка скачалась, отправляем её с текстом в качестве описания
                        with open(image_path, 'rb') as photo:
                            bot.send_photo(CHANNEL_ID, photo, caption=post_text)
                        os.remove(image_path) # Удаляем временный файл
                        print("Пост отправлен с картинкой!")
                    else:
                        # Запасной вариант: если картинки нет, шлем просто текст
                        bot.send_message(CHANNEL_ID, post_text)
                        print("Пост отправлен БЕЗ картинки (не нашли или сбой).")
                        
                    add_to_history(entry.title)
                except Exception as e:
                    print(f"Ошибка отправки в Telegram: {e}")
                    
                time.sleep(2)

if __name__ == "__main__":
    main()
