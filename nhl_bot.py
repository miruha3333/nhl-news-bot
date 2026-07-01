import feedparser
import telebot
import os
import time
import requests
import difflib
from ddgs import DDGS

TOKEN = os.environ.get('TOKEN') 
CHANNEL_ID = '-1004423088204' 
OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY')

# Проверяем оба возможных названия, чтобы исключить ошибку в main.yml
GH_MODELS_TOKEN = os.environ.get('GH_MODELS_TOKEN') or os.environ.get('GITHUB_TOKEN')

bot = telebot.TeleBot(TOKEN)
HISTORY_FILE = "history.txt"

# --- СЛОВАРЬ ИМЕН ---
NAMES_DICT = {
    "Carson Carels": "Карсон Карелс",
    "Alberts Smits": "Альберт Шмидт"
}

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

def is_duplicate(new_text, existing_texts):
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
    Ты — ведущий хоккейный инсайдер и спортивный блогер, пишущий о НХЛ. Твоя задача — превратить сухой английский инсайд в хлёсткий, живой и авторитетный пост для русскоязычных фанатов хоккея.

СТРОГИЕ ПРАВИЛА:
1. Стиль и язык: Никакого машинного перевода и канцелярита. Пиши динамично, используй активный залог и хоккейный контекст (вместо "усилить последний рубеж" пиши "закрыть вратарский вопрос", вместо "изменить баланс сил в обороне" — "прокачать топ-4 защиты").
2. ТАБУ-фразы (ЗАПРЕЩЕНО использовать): «По ситуации с...», «Что касается...», «Вокруг [клуба/игрока]...», «По словам инсайдера», «Руководство внимательно следит/готово действовать». Начинай сразу с сути дела.
3. Авторство: Формат первой строки строго такой: [Имя Автора по-английски]: [Текст поста]. Пример: Chris Johnston: Эдмонтон вовсю ищет... (Слово "Источник" не писать).
4. Имена и команды: Переводи на русский. Названия клубов пиши БЕЗ кавычек и БЕЗ курсива (Эдмонтон, Торонто, Миннесота), просто с заглавной буквы.
5. Борьба с «водой» и объём: Длина текста — строго от 400 до 500 знаков (включая имя автора). Если оригинальный инсайд слишком короткий, НЕ раздувай его банальностями. Вместо этого добавь реальный хоккейный контекст (упомяни дедлайн, проблемы клуба с потолком зарплат, статус игрока (НСА/ОСА) или его роль в звене).
6. Финал: В самой последней строке (с новой строки) напиши строго: SEARCH_QUERY: [Имя главного игрока из текста НА АНГЛИЙСКОМ] NHL photo.

Оригинал: "{clean_text_for_ai}"
    """
    
    # --- ШАГ 1: GPT-4o через GitHub Models (Основной стабильный вариант) ---
    if GH_MODELS_TOKEN:
        try:
            print("🤖 Шаг 1: Пробуем GPT-4o через GitHub Models...")
            url = "https://models.inference.ai.azure.com/chat/completions"
            headers = {"Authorization": f"Bearer {GH_MODELS_TOKEN}", "Content-Type": "application/json"}
            data = {
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": prompt}]
            }
            response = requests.post(url, headers=headers, json=data, timeout=15)
            if response.status_code == 200:
                ai_text = response.json()['choices'][0]['message']['content']
                if ai_text:
                    return ai_text.replace("**", "").replace('"', "").replace("«", "").replace("»", "").strip()
            else:
                print(f"❌ Ошибка Шага 1 (GitHub Models, Статус {response.status_code}): {response.text}")
        except Exception as e:
            print(f"⚠️ Сбой сети GitHub Models: {e}")
            time.sleep(1)

    # --- ШАГ 2: Llama 3.3 70B через OpenRouter (Резервный) ---
    if OPENROUTER_API_KEY:
        try:
            print("🤖 Шаг 2: Пробуем Llama 3.3 70B через OpenRouter...")
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
            data = {
                "model": "meta-llama/llama-3.3-70b-instruct:free",
                "messages": [{"role": "user", "content": prompt}]
            }
            response = requests.post(url, headers=headers, json=data, timeout=15)
            if response.status_code == 200:
                ai_text = response.json()['choices'][0]['message']['content']
                if ai_text:
                    return ai_text.replace("**", "").replace('"', "").replace("«", "").replace("»", "").strip()
            else:
                print(f"❌ Ошибка Шага 2 (OpenRouter Llama, Статус {response.status_code})")
        except Exception as e:
            print(f"⚠️ Сбой сети OpenRouter (Llama): {e}")
            time.sleep(1)

    # --- ШАГ 3: GPT-OSS-120B через OpenRouter (Резервный) ---
    if OPENROUTER_API_KEY:
        try:
            print("🤖 Шаг 3: Пробуем GPT-OSS-120B через OpenRouter...")
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
            data = {
                "model": "openai/gpt-oss-120b:free",
                "messages": [{"role": "user", "content": prompt}]
            }
            response = requests.post(url, headers=headers, json=data, timeout=15)
            if response.status_code == 200:
                ai_text = response.json()['choices'][0]['message']['content']
                if ai_text:
                    return ai_text.replace("**", "").replace('"', "").replace("«", "").replace("»", "").strip()
            else:
                print(f"❌ Ошибка Шага 3 (OpenRouter GPT-OSS, Статус {response.status_code})")
        except Exception as e:
            print(f"⚠️ Сбой сети OpenRouter (GPT-OSS): {e}")
            time.sleep(1)

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
    search_queries = [] 
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
                if query_part:
                    search_queries.append(query_part)
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
        
        for query in search_queries:
            image_path = download_image(query)
            if image_path:
                break
                
        if not image_path and main_search_query:
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
