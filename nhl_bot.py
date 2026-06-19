import g4f
import feedparser
import telebot
import os
import time
import requests
from duckduckgo_search import DDGS

# Вставь свой ID канала
TOKEN = os.environ.get('TOKEN') 
CHANNEL_ID = '-1004423088204' 

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

def escape_html(text):
    """Экранирует спецсимволы, чтобы Telegram не выдавал ошибку разметки"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def download_image(query):
    """Ищет картинки без водяных знаков и пробует скачать несколько вариантов"""
    # Добавляем фильтр против стоковых сайтов с ватермарками
    clean_query = f"{query} -getty -alamy -shutterstock -stock -watermark"
    print(f"Ищем чистую картинку по запросу: {clean_query}")
    
    # Небольшая пауза перед поиском, чтобы поисковик не блокировал за скорость
    time.sleep(3)
    
    try:
        with DDGS() as ddgs:
            results = list(ddgs.images(keywords=clean_query, max_results=3))
            
            for res in results:
                try:
                    img_url = res['image']
                    img_data = requests.get(img_url, timeout=10).content
                    img_name = "temp.jpg"
                    with open(img_name, 'wb') as handler:
                        handler.write(img_data)
                    return img_name 
                except Exception as e:
                    print(f"Не удалось скачать вариант {img_url}: {e}")
                    continue 
    except Exception as e:
        print(f"Ошибка поиска DuckDuckGo: {e}")
        
    return None

def translate_tweet(raw_text):
    # Отрезаем источник и дату с помощью Python
    if ' - ' in raw_text:
        clean_text_for_ai = raw_text.rsplit(' - ', 1)[0]
    else:
        clean_text_for_ai = raw_text

    prompt = f"""
    Ты — автоматический хоккейный редактор. Переведи твит о НХЛ на живой русский язык.
    
    СТРОГИЕ ПРАВИЛА ФОРМАТИРОВАНИЯ ПОСТА:
    1. Формат вывода должен быть строго: Источник: Текст перевода.
    2. Если в начале текста есть автор (например, "Chris Johnston:"), оставь его на английском в начале.
    3. ВАЖНО: Все имена, фамилии хоккеистов и названия команд ПЕРЕВОДИ на русский язык (например: Дилан Ларкин, Алекс Дебринкэт, Детройт, Вегас Голден Найтс).
    4. Убери из текста ВСЕ лишние знаки: кавычки, звездочки. Текст должен быть абсолютно чистым.
    5. Хоккейные термины: Cap hit -> кэпхит, Trade -> обмен, Free agent -> свободный агент.
    6. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО добавлять от себя вводные фразы. Выдай только пост в формате "Источник: Текст".
    7. В самой последней строке ответа напиши строго: SEARCH_QUERY: [Имя главного игрока из текста НА АНГЛИЙСКОМ] NHL photo.
    
    Оригинальный текст: "{clean_text_for_ai}"
    """
    
    for attempt in range(3):
        try:
            response = g4f.ChatCompletion.create(
                model=g4f.models.gpt_4o, 
                messages=[{"role": "user", "content": prompt}]
            )
            if response:
                # Базовая очистка от мусора разметки
                clean_response = response.replace("**", "").replace('"', "").replace("«", "").replace("»", "")
                return clean_response.strip()
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
                # Разделяем текст поста и поисковый запрос
                if "SEARCH_QUERY:" in raw_response:
                    parts = raw_response.split("SEARCH_QUERY:")
                    post_text = parts[0].strip()
                    search_query = parts[1].strip()
                else:
                    post_text = raw_response
                    search_query = None
                
                # Красивое форматирование текста: Автор (жирным) + Новая строка + Текст новости
                post_text = post_text.replace("**", "") # убираем старые звездочки если есть
                if ": " in post_text:
                    author, text_content = post_text.split(": ", 1)
                    formatted_text = f"<b>{escape_html(author.strip())}</b>\n{escape_html(text_content.strip())}"
                else:
                    formatted_text = escape_html(post_text)
                
                image_path = None
                # План А: Ищем фото игрока/команды
                if search_query:
                    image_path = download_image(search_query)
                
                # План Б: Универсальное фото, если План А не сработал
                if not image_path:
                    print("План Б: ищем дефолтную картинку...")
                    image_path = download_image("NHL ice hockey match action")
                
                try:
                    if image_path and os.path.exists(image_path):
                        with open(image_path, 'rb') as photo:
                            # Публикуем фото с HTML-подписью
                            bot.send_photo(CHANNEL_ID, photo, caption=formatted_text, parse_mode='HTML')
                        os.remove(image_path)
                        print("✅ Пост с картинкой отправлен!")
                    else:
                        # Если совсем всё упало — шлем чистый текст
                        bot.send_message(CHANNEL_ID, formatted_text, parse_mode='HTML')
                        print("⚠️ Пост отправлен БЕЗ картинки.")
                        
                    add_to_history(entry.title)
                except Exception as e:
                    print(f"❌ Ошибка отправки в Telegram: {e}")
                    
                # Пауза между постами для стабильности
                time.sleep(5)

if __name__ == "__main__":
    main()
