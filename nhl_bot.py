import os
import sys
import json
import re
import time
import random
import feedparser
import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai.errors import APIError

# Принудительный перевод окружения на UTF-8
os.environ["LC_ALL"] = "C.UTF-8"
os.environ["LANG"] = "C.UTF-8"
os.environ["PYTHONIOENCODING"] = "utf-8"

# ==================== НАСТРОЙКИ (БЕЗОПАСНЫЕ) ====================
# Скрипт автоматически подтянет ключи из переменных окружения (GitHub Secrets)
GEMINI_API_KEY_1 = os.environ.get("GEMINI_API_KEY_1", "")
GEMINI_API_KEY_2 = os.environ.get("GEMINI_API_KEY_2", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# ПОИСК КАРТИНОК
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CSE_CX = os.environ.get("GOOGLE_CSE_CX", "")

OUTPUT_FILE = "hockey_news.txt"
HISTORY_FILE = "history.json"
# ================================================================

# Пул красивых дефолтных картинок (на случай тотального сбоя всех поисковиков)
DEFAULT_HOCKEY_IMAGES = [
    "https://images.unsplash.com/photo-1515523110800-9415d13b84a8?q=80&w=1200", # Шайба на льду
    "https://images.unsplash.com/photo-1580748141549-71748d60bdc9?q=80&w=1200", # Хоккейные коньки/ворота
    "https://images.unsplash.com/photo-1547057416-ba97ef66d8b5?q=80&w=1200", # Арена/матч
    "https://images.unsplash.com/photo-1612872087720-bb876e2e67d1?q=80&w=1200"  # Экипировка/динамика
]

DICTIONARY_FIXES = {
    "Гюнтцель": "Генцел", "Гюнтцеля": "Генцела", "Лафренир": "Лафренье",
    "Ткачак": "Ткачук", "Бифилд": "Байфилд", "Мэтьюс": "Мэттьюс",
    "Юта Маммут": "Юта", "Юты Маммут": "Юты"
}
# ============================================================================

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"processed_urls": [], "recent_titles": []}

def save_history(history):
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=4)

def clean_text_with_dict(text):
    for wrong, right in DICTIONARY_FIXES.items():
        text = re.sub(r' ' + wrong + r' ', right, text)
    return text

# --- БЛОК ПОИСКА КАРТИНОК (ОФИЦИАЛЬНЫЙ GOOGLE API + ФОЛЛБЕКИ) ---

def search_google_images(query):
    """Метод №1: Официальный Google Custom Search API (Бесплатно 100 запросов в сутки)"""
    if not GOOGLE_API_KEY or "ТВОЙ" in GOOGLE_API_KEY or not GOOGLE_CSE_CX or "ТВОЙ" in GOOGLE_CSE_CX:
        return None
    
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CSE_CX,
        "q": query,
        "searchType": "image",
        "num": 1,
        "safe": "active"
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if "items" in data and len(data["items"]) > 0:
                return data["items"][0]["link"]
        elif response.status_code == 429:
            print("   [!] Google Images API: Исчерпан суточный лимит (429).")
    except Exception as e:
        print(f"   [!] Ошибка Google Images API: {e}")
    return None

def search_duckduckgo_images(query):
    """Метод №2: DuckDuckGo с обходом блокировок (User-Agent и заголовки)"""
    # Так как старая библиотека упала, делаем аккуратный прямой запрос с маскировкой под браузер
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }
    try:
        # Сначала получаем vqd токен, который требует DuckDuckGo
        token_url = f"https://duckduckgo.com/?q={requests.utils.quote(query)}"
        res = requests.get(token_url, headers=headers, timeout=10)
        vqd_match = re.search(r"vqd=([0-9-]+)&", res.text)
        if not vqd_match:
            vqd_match = re.search(r'vqd\s*=\s*["']([0-9-]+)["']', res.text)
            
        if vqd_match:
            vqd = vqd_match.group(1)
            img_url = "https://duckduckgo.com/i.js"
            params = {"o": "json", "q": query, "vqd": vqd, "f": ",,,,,", "p": "1"}
            img_res = requests.get(img_url, headers=headers, params=params, timeout=10)
            if img_res.status_code == 200:
                data = img_res.json()
                if "results" in data and len(data["results"]) > 0:
                    return data["results"][0]["image"]
    except Exception:
        pass
    return None

def get_smart_image(player_name, team_context="NHL"):
    """Главный диспетчер картинок: собирает поисковый запрос и ищет его по каскаду"""
    # Формируем чистый запрос без водяных знаков
    clean_query = f"{player_name} {team_context} match photo -getty -alamy -shutterstock -stock -watermark"
    print(f"   -> Ищем картинку для: {player_name}...")
    
    # 1. Пробуем Google API
    img = search_google_images(clean_query)
    if img: 
        print("      Успешно найдено через Google Images!")
        return img
        
    # 2. Если Google пуст/лимит, пробуем замаскированный DuckDuckGo
    print("      Google недоступен. Пробуем резервный DuckDuckGo...")
    img = search_duckduckgo_images(clean_query)
    if img:
        print("      Успешно найдено через DuckDuckGo!")
        return img
        
    # 3. Тотальный фоллбек — берем случайное крутое хоккейное фото из пула
    default_img = random.choice(DEFAULT_HOCKEY_IMAGES)
    print(f"      [!] Все поисковики заблокированы. Используем базовое фото: {default_img}")
    return default_img

# --- БЛОК НЕЙРОСЕТЕЙ (ТЕКСТ) ---

def get_prompts(article, recent_titles):
    system_prompt = f"""
    Ты — профессиональный хоккейный журналист. Твоя задача — перевести новость на русский язык и адаптировать ее под формат постов в Telegram.

    СТРОГИЕ ПРАВИЛА:
    1. Термины: "кэпхит" -> "сумма контракта"; "оборонец" -> "защитник"; "Utah/Utah Mammoths" -> "Юта"; "no-trade clause" -> "полный запрет на обмен". "переход на Остров" -> "переход в стан Островитян".
    2. ФАКТЫ: Пиши ТОЛЬКО ту информацию, которая есть в тексте. ЗАПРЕЩЕНО брать факты, даты или статистику из своей головы или интернета.
    3. ОБЪЕМ: Каждая новость строго от 400 до 500 символов. Если инфы мало, разверни мысль автора более глубоким русским литературным языком.
    4. РАЗДЕЛЕНИЕ: Если текст содержит слухи про РАЗНЫЕ команды/игроков, разбей их на отдельные посты. Разделяй их строкой: ===
    5. ФОРМАТ ПОСТА: Каждый пост начинается строго с субъекта, от которого исходит новость, выделенного жирным шрифтом, и точки. Пример: **Марк Эассон.** или **Пьер ЛеБрюн.** Никаких слов "Источник:" или "Автор:" в тексте быть категорически не должно! Только само имя.
    6. ИГРОК: В САМОЙ ПОСЛЕДНЕЙ СТРОКЕ поста напиши имя главного героя новости на английском языке для поиска картинки, строго в формате: КАРТИНКА: Имя Фамилия. Пример: КАРТИНКА: Connor McDavid. Если главных героев несколько или это общий слух про команду, напиши название команды или лиги, например: КАРТИНКА: Philadelphia Flyers.
    7. ДУБЛИ: Если вся новость целиком по смыслу совпадает с тем, что уже было в истории ({recent_titles}), верни только одно слово: ДУБЛЬ.
    """
    user_prompt = f"Данные автора/источника: {article['source']}
Текст новости: {article['content']}"
    return system_prompt, user_prompt

def ask_gemini(api_key, system_prompt, user_prompt):
    if not api_key or "ТВОЙ" in api_key: return None
    try:
        client = genai.Client(api_key=api_key)
        full_prompt = system_prompt + "

" + user_prompt
        response = client.models.generate_content(model="gemini-2.5-flash", contents=full_prompt)
        return response.text.strip()
    except APIError as e:
        if e.code == 429: return "LIMIT"
        return None
    except Exception:
        return None

def ask_groq(api_key, system_prompt, user_prompt):
    if not api_key or "ТВОЙ" in api_key: return None
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "llama3-8b-8192",
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]
    }
    try:
        response = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=15)
        if response.status_code == 200: return response.json()['choices'][0]['message']['content'].strip()
        elif response.status_code == 429: return "LIMIT"
    except Exception: pass
    return None

def ask_github(api_key, system_prompt, user_prompt):
    if not api_key or "ТВОЙ" in api_key: return None
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]
    }
    try:
        response = requests.post("https://models.inference.ai.azure.com/chat/completions", headers=headers, json=payload, timeout=15)
        if response.status_code == 200: return response.json()['choices'][0]['message']['content'].strip()
        elif response.status_code == 429: return "LIMIT"
    except Exception: pass
    return None

def process_news_with_fallback(article, recent_titles_str):
    system_prompt, user_prompt = get_prompts(article, recent_titles_str)
    
    res = ask_gemini(GEMINI_API_KEY_1, system_prompt, user_prompt)
    if res and res != "LIMIT": return res

    if GEMINI_API_KEY_2:
        res = ask_gemini(GEMINI_API_KEY_2, system_prompt, user_prompt)
        if res and res != "LIMIT": return res

    if GROQ_API_KEY:
        res = ask_groq(GROQ_API_KEY, system_prompt, user_prompt)
        if res and res != "LIMIT": return res

    if GITHUB_TOKEN:
        res = ask_github(GITHUB_TOKEN, system_prompt, user_prompt)
        if res and res != "LIMIT": return res

    return None

# --- ПАРСЕРЫ СЛЕНТ ---

def parse_nhl_rumors():
    feed = feedparser.parse("https://nhlrumors.com/feed/")
    articles = []
    for entry in feed.entries:
        author = entry.get('author', 'NHL Rumors')
        full_html = entry.content[0].value if 'content' in entry else entry.summary
        clean_text = BeautifulSoup(full_html, "html.parser").get_text()
        articles.append({"title": entry.title, "link": entry.link, "source": author, "content": clean_text})
    return articles

def parse_hockey_feed():
    url = "https://www.hockeyfeed.com/nhl-news"
    articles = []
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            blocks = soup.find_all('a', href=re.compile(r'/nhl-news/'))
            for block in blocks[:10]:
                link = block['href']
                if not link.startswith('http'): link = 'https://www.hockeyfeed.com' + link
                raw_title = block.get_text().strip()
                clean_title = re.sub(r'^(Jonathan Larivee|Chris Gosselin|Published).*?ago\s*', '', raw_title, flags=re.IGNORECASE)
                author = "Hockey Feed"
                if "Jonathan Larivee" in raw_title: author = "Джонатан Лариве"
                elif "Chris Gosselin" in raw_title: author = "Крис Госселин"
                if len(clean_title) > 15:
                    articles.append({"title": clean_title, "link": link, "source": author, "content": clean_title})
    except Exception: pass
    return articles

def main():
    print("[Парсер Скрипт Включен]")
    history = load_history()
    is_first_run = len(history["processed_urls"]) == 0
    all_articles = parse_nhl_rumors() + parse_hockey_feed()
    new_posts_count = 0
    
    for article in all_articles:
        url = article['link']
        if url in history["processed_urls"]: continue
            
        if is_first_run and new_posts_count >= 3:
            history["processed_urls"].append(url)
            history["recent_titles"].append(article['title'])
            continue
            
        print(f"
[Обработка] Новость: {article['title'][:50]}...")
        recent_titles_str = ", ".join(history["recent_titles"][-15:])
        
        final_text = process_news_with_fallback(article, recent_titles_str)
        if not final_text: continue
            
        if "ДУБЛЬ" in final_text.upper() and len(final_text) < 10:
            history["processed_urls"].append(url)
            continue
            
        history["processed_urls"].append(url)
        sub_posts = final_text.split("===")
        
        with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
            for post in sub_posts:
                clean_post = post.strip()
                if len(clean_post) < 40: continue
                
                # Извлекаем сущность для поиска картинки
                player_search_name = "NHL ice hockey"
                image_match = re.search(r"КАРТИНКА:\s*(.*?)$", clean_post, re.IGNORECASE)
                if image_match:
                    player_search_name = image_match.group(1).strip()
                    # Удаляем техническую строку из финального текста поста
                    clean_post = clean_post.replace(image_match.group(0), "").strip()
                
                # Запускаем бессмертный поиск картинки
                image_url = get_smart_image(player_search_name)
                
                # Очищаем финальный русский текст через словарь
                clean_post = clean_text_with_dict(clean_post)
                
                f.write(f"ФОТО ССЫЛКА: {image_url}
")
                f.write(f"{clean_post}
")
                f.write("-" * 50 + "

")
                new_posts_count += 1
                print(f"   [+] Пост и картинка успешно сохранены!")
            
        history["recent_titles"].append(article['title'])
        time.sleep(2)
        
    save_history(history)
    print(f"
[Готово] Работа завершена. Создано постов: {new_posts_count}")

if __name__ == "__main__":
    main()
