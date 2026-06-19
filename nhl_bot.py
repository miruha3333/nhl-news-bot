import g4f
import feedparser
import telebot
import os
import time
import requests
from duckduckgo_search import DDGS

# Вставь свой ID канала
TOKEN = os.environ.get('TOKEN') 
CHANNEL_ID = '-100XXXXXXXXXX' 

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
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def download_image(query):
    """Ищет чистые картинки и строго фильтрует форматы JPG/PNG"""
    clean_query = f"{query} -getty -alamy -shutterstock -stock -watermark"
    print(f"Ищем чистую картинку по запросу: {clean_query}")
    time.sleep(2)
    
    try:
        with DDGS() as ddgs:
            # Берем топ-5 результатов, чтобы точно найти подходящий формат
            results = list(ddgs.images(keywords=clean_query, max_results=5))
            for res in results:
                try:
                    img_url = res['image']
                    
                    # Скачиваем картинку
                    response = requests.get(img_url, timeout=10)
                    
                    # Получаем реальный формат файла из заголовков сервера
                    content_type = response.headers.get('Content-Type', '').lower()
                    
                    # СТРОГАЯ ПРОВЕРКА: разрешаем только JPEG и PNG
                    if 'image/jpeg' in content_type:
                        img_name = "temp.jpg"
                    elif 'image/png' in content_type:
                        img_name = "temp.png"
                    else:
                        print(f"Пропускаем: неподдерживаемый Telegram формат ({content_type}) для {img_url}")
                        continue # Переходим к следующей картинке
                    
                    # Если проверка пройдена, сохраняем файл
                    with open(img_name, 'wb') as handler:
                        handler.write(response.content)
                    print(f"Успешно скачан подходящий формат: {img_name}")
                    return img_name 
                    
                except Exception as e:
                    print(f"Не удалось обработать вариант {img_url}: {e}")
                    continue 
    except Exception as e:
        print(f"Ошибка поиска: {e}")
        
    return None

def translate_tweet(raw_text):
    if ' - ' in raw_text:
        clean_text_for_ai = raw_text.rsplit(' - ', 1)[0]
    else:
        clean_text_for_ai = raw_text

    prompt = f"""
    Ты — автоматический хоккейный редактор. Переведи твит о НХЛ на живой русский язык.
    
    СТРОГИЕ ПРАВИЛА ФОРМАТИРОВАНИЯ ПОСТА:
    1. Если в оригинале указан автор (например, "Chris Johnston:" или "David Pagnotta:"), начни перевод с его имени на английском, поставь двоеточие и пробел. Слово "Источник" писать КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО.
    2. ВАЖНО: Все имена, фамилии хоккеистов и названия команд ПЕРЕВОДИ на русский язык (например: Дилан Ларкин, Алекс Дебринкэт, Детройт).
    3. Убери из текста ВСЕ лишние знаки: кавычки, звездочки. Текст должен быть абсолютно чистым.
    4. Хоккейные термины: Cap hit -> кэпхит, Trade -> обмен, Free agent -> свободный агент.
    5. Выдай только готовый текст. Никаких вводных фраз.
    6. В самой последней строке ответа напиши строго: SEARCH_QUERY: [Имя главного игрока из текста НА АНГЛИЙСКОМ] NHL photo.
    
    Оригинальный текст: "{clean_text_for_ai}"
    """
    
    for attempt in range(3):
        try:
            response = g4f.ChatCompletion.create(
                model=g4f.models.gpt_4o, 
                messages=[{"role": "user", "content": prompt}]
            )
            if response:
                clean_response = response.replace("**", "").replace('"', "").replace("«", "").replace("»", "")
                return clean_response.strip()
        except:
            time.sleep(2)
    return None

def main():
    feed = feedparser.parse("https://nitter.net/NHLRumourReport/rss")
    history = get_history()
    
    new_entries = []
    for entry in reversed(feed.entries[:5]):
        if entry.title not in history:
            new_entries.append(entry)
            
    if not new_entries:
        print("Новых постов нет.")
        return
        
    combined_texts = []
    main_search_query = None
    entries_to_save = []
    
    for entry in new_entries:
        raw_response = translate_tweet(entry.title)
        
        if raw_response:
            if "SEARCH_QUERY:" in raw_response:
                parts = raw_response.split("SEARCH_QUERY:")
                post_text = parts[0].strip()
                if not main_search_query and len(parts) > 1:
                    main_search_query = parts[1].strip()
            else:
                post_text = raw_response
            
            if ": " in post_text:
                author, text_content = post_text.split(": ", 1)
                author = author.replace("Источник", "").strip() 
                formatted_text = f"<b>{escape_html(author)}:</b> {escape_html(text_content)}"
            else:
                formatted_text = escape_html(post_text)
                
            combined_texts.append(formatted_text)
            entries_to_save.append(entry.title)
            
    if combined_texts:
        final_post = "\n\n".join(combined_texts)
        
        image_path = None
        if main_search_query:
            image_path = download_image(main_search_query)
            
        if not image_path:
            print("План Б: ищем дефолтную картинку...")
            image_path = download_image("NHL ice hockey match action")
        
        try:
            image_sent = False
            if image_path and os.path.exists(image_path):
                try:
                    with open(image_path, 'rb') as photo:
                        if len(final_post) <= 1024:
                            bot.send_photo(CHANNEL_ID, photo, caption=final_post, parse_mode='HTML')
                        else:
                            bot.send_photo(CHANNEL_ID, photo)
                            bot.send_message(CHANNEL_ID, final_post, parse_mode='HTML')
                    image_sent = True
                except Exception as e:
                    print(f"⚠️ Telegram все равно отклонил этот файл: {e}")
                finally:
                    if os.path.exists(image_path):
                        os.remove(image_path)
            
            if not image_sent:
                bot.send_message(CHANNEL_ID, final_post, parse_mode='HTML')
                print("✅ Пост отправлен БЕЗ картинки (сработал запасной план).")
            else:
                print("✅ Сводный пост с валидной картинкой отправлен успешно!")
                
            for title in entries_to_save:
                add_to_history(title)
                
        except Exception as e:
            print(f"❌ Критическая ошибка отправки: {e}")

if __name__ == "__main__":
    main()
