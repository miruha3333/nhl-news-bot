import feedparser
import telebot
import os
import time
import requests
import difflib
from google import genai
from ddgs import DDGS

TOKEN = os.environ.get('TOKEN') 
CHANNEL_ID = '-100XXXXXXXXXX' 

bot = telebot.TeleBot(TOKEN)
HISTORY_FILE = "history.txt"

# Инициализируем официального клиента Gemini
# Он автоматически подтянет переменную GEMINI_API_KEY из окружения GitHub Actions
gemini_client = genai.Client()

# --- СЛОВАРЬ ИМЕН ---
NAMES_DICT = {
    "Carson Carels": "Карсон Кулеш",
    "Alberts Smits": "Альберт Шмидт"
}

def get_history():
    if not os.path.exists(HISTORY_FILE): 
        return set()
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f)

def add_to_history(title):
    """Добавляет новость и оставляет в файле только последние 100 актуальных записей"""
    lines = []
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
    
    if title not in lines:
        lines.append(title)
    
    lines = lines[-100:]
    
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")

def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def is_duplicate(new_text, existing_texts):
    """Проверяет, нет ли в посте слишком похожего текста (защита от дублей)"""
    for text in existing_texts:
        similarity = difflib.SequenceMatcher(None, new_text, text).ratio()
        if similarity > 0.8:
            return True
    return False

def download_image(query):
    clean_query = f"{query} -getty -alamy -shutterstock -stock -watermark"
    print(f"Ищем чистую картинку по запросу: {clean_query}")
    time.sleep(2)
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    }
    
    bad_url_words = ['alamy', 'getty', 'shutterstock', 'depositphotos', 'stock', 'dreamstime']
    
    try:
        with DDGS() as ddgs:
            results = list(ddgs.images(query=clean_query, max_results=15))
            
            for res in results:
                try:
                    img_url = res['image'].lower()
                    
                    if any(bad in img_url for bad in bad_url_words):
                        print(f"Пропуск (копирайт в URL): {img_url}")
                        continue
                        
                    response = requests.get(res['image'], headers=headers, timeout=10)
                    if response.status_code != 200:
                        continue
                        
                    content_type = response.headers.get('Content-Type', '').lower()
                    if 'image/jpeg' in content_type or 'image/jpg' in content_type:
                        img_name = "temp.jpg"
                    elif 'image/png' in content_type:
                        img_name = "temp.png"
                    else:
                        continue 
                    
                    if len(response.content) < 5000:
                        continue

                    with open(img_name, 'wb') as handler:
                        handler.write(response.content)
                    print(f"Успешно скачан рабочий файл: {img_name}")
                    return img_name 
                    
                except Exception as e:
                    continue 
    except Exception as e:
        print(f"Ошибка поиска картинок: {e}")
        
    return None

def preprocess_text(text):
    for eng_name, rus_name in NAMES_DICT.items():
        text = text.replace(eng_name, rus_name)
    return text

def translate_tweet(raw_text):
    clean_text = preprocess_text(raw_text)
    
    if ' - ' in clean_text:
        clean_text_for_ai = clean_text.rsplit(' - ', 1)[0]
    else:
        clean_text_for_ai = clean_text

    prompt = f"""
    Ты — профессиональный хоккейный журналист, редактор и эксперт по НХЛ. Переведи инсайд на безупречный, живой и литературный русский язык.

    СТРОГИЕ ПРАВИЛА:
    1. Качество: Никакого машинного перевода! Строй предложения логично. Текст должен быть авторитетным и серьезным. 
    2. Автор: Если в оригинале указан автор (напр. "Chris Johnston:"), начни с его имени по-английски и поставь двоеточие. Слово "Источник" писать КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО.
    3. Имена: Переводи все имена, фамилии хоккеистов и названия команд на русский язык.
    4. Очистка от мусора: Безжалостно УДАЛЯЙ названия радиошоу, подкастов, приписки в духе "Мелник ин зе Афтернун", "Fourth Period" и даты в конце текста. Оставляй только саму хоккейную новость.
    5. Выдай только готовый текст.
    6. В самой последней строке (с новой строки) напиши строго: SEARCH_QUERY: [Имя главного игрока из текста НА АНГЛИЙСКОМ] NHL photo.
    
    Оригинал: "{clean_text_for_ai}"
    """
    
    for attempt in range(3):
        try:
            # Делаем официальный запрос к актуальной модели Gemini
            response = gemini_client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
            )
            if response and response.text:
                return response.text.replace("**", "").replace('"', "").replace("«", "").replace("»", "").strip()
        except Exception as e:
            print(f"Ошибка Gemini (попытка {attempt+1}): {e}")
            time.sleep(2)
    return None

def main():
    feed = feedparser.parse("https://nitter.net/NHLRumourReport/rss")
    history = get_history()
    
    new_entries = []
    for entry in reversed(feed.entries[:10]):
        if entry.title not in history:
            new_entries.append(entry)
            
    if not new_entries:
        print("Новых постов нет.")
        return
        
    combined_texts = []
    pure_texts_for_diff = [] 
    main_search_query = None
    entries_to_save = []
    
    for entry in new_entries:
        raw_response = translate_tweet(entry.title)
        
        if raw_response:
            if "SEARCH_QUERY:" in raw_response:
                idx = raw_response.rfind("SEARCH_QUERY:")
                post_text = raw_response[:idx].strip()
                query_part = raw_response[idx:].replace("SEARCH_QUERY:", "").strip()
                if not main_search_query and query_part:
                    main_search_query = query_part
            else:
                post_text = raw_response
            
            if ": " in post_text:
                author, text_content = post_text.split(": ", 1)
                author = author.replace("Источник", "").strip() 
                
                if is_duplicate(text_content, pure_texts_for_diff):
                    print(f"Найден дубль, пропускаем: {text_content[:30]}...")
                    entries_to_save.append(entry.title) 
                    continue
                
                pure_texts_for_diff.append(text_content)
                formatted_text = f"<b>{escape_html(author)}</b>\n{escape_html(text_content)}"
            else:
                if is_duplicate(post_text, pure_texts_for_diff):
                    entries_to_save.append(entry.title)
                    continue
                pure_texts_for_diff.append(post_text)
                formatted_text = escape_html(post_text)
                
            combined_texts.append(formatted_text)
            entries_to_save.append(entry.title)
            
    if combined_texts:
        final_post = "\n\n".join(combined_texts)
        
        image_path = None
        if main_search_query:
            image_path = download_image(main_search_query)
            
        if not image_path:
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
                    print(f"⚠️ Telegram отклонил файл: {e}")
                finally:
                    if os.path.exists(image_path):
                        os.remove(image_path)
            
            if not image_sent:
                bot.send_message(CHANNEL_ID, final_post, parse_mode='HTML')
            
            for title in entries_to_save:
                add_to_history(title)
                
        except Exception as e:
            print(f"❌ Ошибка отправки: {e}")

if __name__ == "__main__":
    main()
