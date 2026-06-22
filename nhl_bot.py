import os
import time
import feedparser
import telebot
import requests
from openai import OpenAI
import google.generativeai as genai

# --- 1. НАСТРОЙКИ ДОСТУПА И ПРОВЕРКА СЕКРЕТОВ ---
TOKEN = os.environ.get('TOKEN') or os.environ.get('TELEGRAM_BOT_TOKEN')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
GH_MODELS_TOKEN = os.environ.get('GH_MODELS_TOKEN')

# !!! ВСТАВЬ СЮДА ID СВОЕГО ТЕЛЕГРАМ-КАНАЛА !!!
CHANNEL_ID = '-100XXXXXXXXXX' 

# Жесткие проверки, чтобы скрипт не падал с непонятными ошибками библиотек
if not TOKEN or ':' not in str(TOKEN):
    raise ValueError("❌ Критическая ошибка: Токен Telegram отсутствует или не содержит двоеточия!")
if not GROQ_API_KEY and not GEMINI_API_KEY and not GH_MODELS_TOKEN:
    raise ValueError("❌ Критическая ошибка: Ни один API-ключ для нейросетей не найден!")

bot = telebot.TeleBot(TOKEN)
HISTORY_FILE = "history.txt"

# --- 2. НАСТРОЙКА КЛИЕНТОВ ИИ ---

# Клиент Groq (использует совместимость с OpenAI)
groq_client = None
if GROQ_API_KEY:
    groq_client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")

# Клиент Gemini
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# Клиент GitHub Models (использует совместимость с OpenAI, но свой URL)
gh_client = None
if GH_MODELS_TOKEN:
    gh_client = OpenAI(api_key=GH_MODELS_TOKEN, base_url="https://models.inference.ai.azure.com")

RSS_FEEDS = [
    "https://www.nhl.com/flyers/rss.xml",
    "https://www.nhl.com/penguins/rss.xml",
    "https://www.nhl.com/sharks/rss.xml",
    "https://hockeyfeed.com/rss",
    "https://nhlrumors.com/feed/"
]

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return set()
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())

def save_to_history(link):
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(link + "\n")

# --- 3. СИСТЕМА ОТКАЗОУСТОЙЧИВОСТИ (FAILOVER) ---

def ask_groq(prompt):
    if not groq_client: raise Exception("Groq не настроен")
    response = groq_client.chat.completions.create(
        model='llama-3.3-70b-versatile',
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7
    )
    return response.choices[0].message.content.strip()

def ask_gemini(prompt):
    if not GEMINI_API_KEY: raise Exception("Gemini не настроен")
    model = genai.GenerativeModel('gemini-1.5-flash')
    response = model.generate_content(prompt)
    return response.text.strip()

def ask_github_models(prompt):
    if not gh_client: raise Exception("GitHub Models не настроен")
    response = gh_client.chat.completions.create(
        model='gpt-4o-mini',
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7
    )
    return response.choices[0].message.content.strip()

def summarize_and_translate(title, description):
    """
    Функция пытается перевести текст, переключаясь между провайдерами при ошибках или лимитах.
    Порядок: 1. Groq -> 2. Gemini -> 3. GitHub Models
    """
    prompt = f"Переведи на русский язык и сделай краткую, интересную выжимку (2-4 предложения) для хоккейного телеграм-канала. Оформляй красиво. Новость:\n\nЗаголовок: {title}\nТекст: {description}"
    
    # Попытка 1: Groq (Самый быстрый)
    try:
        print("Пробуем через Groq...")
        return ask_groq(prompt)
    except Exception as e:
        print(f"⚠️ Groq недоступен ({e}). Переключаемся на Gemini...")
        time.sleep(1)
        
    # Попытка 2: Gemini
    try:
        print("Пробуем через Gemini...")
        return ask_gemini(prompt)
    except Exception as e:
        print(f"⚠️ Gemini недоступен ({e}). Переключаемся на GitHub Models...")
        time.sleep(1)

    # Попытка 3: GitHub Models (Fall-back)
    try:
        print("Пробуем через GitHub Models...")
        return ask_github_models(prompt)
    except Exception as e:
        print(f"❌ Все три провайдера недоступны. Ошибка: {e}")
        return None

# --- 4. ОСНОВНОЙ ЦИКЛ ---

def main():
    print("Бот запущен. Проверяем хоккейные новости...")
    history = load_history()
    new_posts_count = 0

    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:5]:
                link = entry.link
                
                if link in history:
                    continue

                title = entry.get('title', '')
                description = entry.get('summary', entry.get('description', ''))
                
                print(f"Найдена новость: {title}")
                
                final_text = summarize_and_translate(title, description)
                
                if final_text:
                    telegram_message = f"{final_text}\n\n🔗 [Источник]({link})"
                    
                    bot.send_message(CHANNEL_ID, telegram_message, parse_mode='Markdown', disable_web_page_preview=False)
                    print("✅ Пост успешно отправлен в канал!")
                    
                    save_to_history(link)
                    history.add(link)
                    new_posts_count += 1
                    time.sleep(3) # Задержка для Telegram API
                    
        except Exception as e:
            print(f"Ошибка при обработке ленты {feed_url}: {e}")

    print(f"Проверка завершена. Отправлено новых постов: {new_posts_count}")

if __name__ == "__main__":
    main()
