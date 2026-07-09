import feedparser
import telebot
import os
import time
import requests
import difflib
import pymorphy3
import re
from ddgs import DDGS

TOKEN = os.environ.get('TOKEN') 
CHANNEL_ID = '-1004423088204' 
OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY')

# Проверяем оба возможных названия, чтобы исключить ошибку в main.yml
GH_MODELS_TOKEN = os.environ.get('GH_MODELS_TOKEN') or os.environ.get('GITHUB_TOKEN')

bot = telebot.TeleBot(TOKEN)
HISTORY_FILE = "history.txt"
morph = pymorphy3.MorphAnalyzer()

# --- БИБЛИОТЕКА ПРАВИЛЬНЫХ ИМЕН ---
NAMES_DICT = {
    "Mason Marchment": "Мэйсон Марчмент",
    "J.J. Peterka": "Дж. Дж. Петерка",
    "JJ Peterka": "Дж. Дж. Петерка",
    "Zach Werenski": "Зак Веренски",
    "Morgan Rielly": "Морган Райлли",
    "Darnell Nurse": "Дарнелл Нерс",
    "Jacob Trouba": "Джейкоб Троуба",
    "Nick Jensen": "Ник Йенсен",
    "Connor Hellebuyck": "Коннор Хеллебайк",
    "Pat Verbeek": "Пэт Вербик",
    "Carson Carels": "Карсон Карелс",
    "Alberts Smits": "Альберт Шмидт",
    "Claude Giroux": "Клод Жиру",
    "Connor Bedard": "Коннор Бедард",
    "Leo Carlsson": "Лео Карлссон",
    "Jake DeBrusk": "Джейк Дебраск"
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

def clean_bot_hallucinations(text):
    """
    Жесткий Python-фильтр. Если нейросеть проигнорировала правила в промпте, 
    этот блок принудительно вырежет и заменит все ошибки синтаксиса и прозвища.
    """
    if not text:
        return text
    
    replacements = {
        "гендир": "генеральный менеджер",
        "дженерал менеджер": "генеральный менеджер",
        "дженерал": "генеральный менеджер",
        "чигагских ястребов": "Чикаго",
        "чикагские ястребы": "Чикаго",
        "чикагских ястребах": "Чикаго",
        "у ястребов": "у Чикаго",
        "ястребы": "Чикаго",
        "ястребов": "Чикаго",
        "Лио Карлссон": "Лео Карлссон",
        "Лио Карлссона": "Лео Карлссона",
        "Лио Карлссону": "Лео Карлссону",
        "Дарнеллы Нерса": "Дарнелла Нерса",
        "сработать сделка": "провернуть сделку"
    }
    
    for bad_word, good_word in replacements.items():
        text = re.sub(r'\b' + re.escape(bad_word) + r'\b', good_word, text, flags=re.IGNORECASE)
        
    return text

def fix_sports_grammar(text):
    """Исправляет падежные окончания после предлогов."""
    if not text: return text
    words = text.split()
    prep_cases = {
        "за": "ablt", "к": "datv", "ко": "datv", "для": "gent",
        "от": "gent", "до": "gent", "из": "gent", "у": "gent",
        "о": "loct", "об": "loct", "с": "ablt", "со": "ablt"
    }
    for i in range(len(words) - 1):
        clean_prep = words[i].lower().strip(",.?!()\"«»")
        if clean_prep in prep_cases:
            target_case = prep_cases[clean_prep]
            for j in range(1, 3):
                if i + j < len(words):
                    word_with_punct = words[i+j]
                    clean_word = word_with_punct.strip(",.?!()\"«»")
                    if not clean_word: continue
                    is_name = any(clean_word.lower() in rus.lower() for eng, rus in NAMES_DICT.items()) or clean_word[0].isupper()
                    if is_name:
                        parsed = morph.parse(clean_word)[0]
                        inflected = parsed.inflect({target_case})
                        if inflected:
                            corrected_word = inflected.word.capitalize()
                            prefix = re.match(r"^[^a-zA-Zа-яА-ЯёЁ]*", word_with_punct).group(0)
                            suffix = re.search(r"[^a-zA-Zа-яА-ЯёЁ]*$", word_with_punct).group(0)
                            words[i+j] = prefix + corrected_word + suffix
    return " ".join(words)

def download_image(query):
    clean_query = f"{query} -getty -alamy -shutterstock -stock -watermark"
    time.sleep(2)
    headers = {"User-Agent": "Mozilla/5.0"}
    bad_url_words = ['alamy', 'getty', 'shutterstock', 'depositphotos', 'stock', 'dreamstime']
    try:
        with DDGS() as ddgs:
            results = list(ddgs.images(query=clean_query, max_results=15))
            for res in results:
                img_url = res['image'].lower()
                if any(bad in img_url for bad in bad_url_words): continue
                response = requests.get(res['image'], headers=headers, timeout=10)
                if response.status_code == 200:
                    with open("temp.png", 'wb') as handler: handler.write(response.content)
                    return "temp.png"
    except Exception as e:
        print(f"Ошибка картинки: {e}")
    return None

def translate_tweet(raw_text):
    clean_text_for_ai = raw_text.rsplit(' - ', 1)[0] if ' - ' in raw_text else raw_text

    # Подготовка глоссария падежей
    glossary_lines = []
    for eng, rus in NAMES_DICT.items():
        try:
            words = rus.split()
            forms = []
            for case in ['gent', 'datv', 'ablt']:
                inflected_words = []
                for w in words:
                    parsed = morph.parse(w)[0]
                    inflected = parsed.inflect({case})
                    inflected_words.append(inflected.word.capitalize() if inflected else w)
                forms.append(" ".join(inflected_words))
            glossary_lines.append(f"- {eng} -> {rus} (кого/чего: {forms[0]}, кому/чему: {forms[1]}, кем/чем: {forms[2]})")
        except Exception:
            glossary_lines.append(f"- {eng} -> {rus}")
    names_glossary = "\n".join(glossary_lines)

    prompt = f"""
    Ты — ведущий хоккейный инсайдер и спортивный блогер, пишущий о НХЛ. Твоя задача — адаптировать сухой английский инсайд в хлёсткий, живой и авторитетный пост для русскоязычных фанатов хоккея.

СТРОГИЕ ПРАВИЛА:
1. ЖЕСТКАЯ ФАКТОЛОГИЯ (ГЛАВНОЕ ПРАВИЛО): Передавай ТОЛЬКО ту информацию, которая есть в оригинальном тексте. 
   - СТРОГО ЗАПРЕЩЕНО выдумывать новые факты, предыстории или контекст.
   - СТРОГО ЗАПРЕЩЕНО использовать свои фоновые знания об игроках или командах (если в тексте нет упоминания КХЛ, СКА, возраста игрока или его личной жизни — ты не имеешь права это писать).
   - Если оригинальный инсайд короткий — твой пост должен быть таким же коротким. Никакой лишней «воды».

2. ИМЕНА, КОМАНДЫ И АББРЕВИАТУРА:
   - Используй ТОЛЬКО официальные полные названия команд или городов (Чикаго, Эдмонтон, Торонто, Виннипег). 
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать прозвища команд (никаких "ястребов", "листьев", "нефтяников", "сенаторов" и т.д.).
   - Имя Leo переводится строго как Лео. Имя Darnell — как Дарнелл. Склоняй имена строго по правилам русского языка. Если сомневаешься в склонении — оставляй имя в начальной форме, но не коверкай.
   - АББРЕВИАТУРЫ: GM -> генеральный менеджер (НИКОГДА не пиши "гендир" или "дженерал"). HC -> главный тренер. NTC/NMC -> пункт о запрете на обмен.

3. ПРАВИЛО СИНТАКСИСА: Перевод должен быть естественным для русского языка. Не копируй английскую структуру фраз. Избегай глупых ломаных конструкций.

ПРИМЕРЫ КАТЕГОРИЧЕСКИ ЗАПРЕЩЕННОГО ПЕРЕВОДА (ТАКОЙ БРЕД ПИСАТЬ НЕЛЬЗЯ):
- Английский оригинал: "Darnell Nurse situation in Edmonton..."
  ❌ Плохой перевод: "История у Дарнеллы Нерса и Эдмонтона..." (Ошибка: имя Darnell соткано как женское. Правильно: ситуация с Дарнеллом Нерсом).
- Английский оригинал: "Chicago GM Kyle Davidson started talks..."
  ❌ Плохой перевод: "Гендир Дженерал Кайл Дэвидсон..." (Ошибка: бред в аббревиатуре GM. Правильно: Генеральный менеджер Чикаго Кайл Дэвидсон).
- Английский оригинал: "Chicago Blackhawks are on alert..."
  ❌ Плохой перевод: "У чикагских ястребов сейчас одна задача..." (Ошибка: использовано прозвище "ястребы". Правильно: У Чикаго сейчас одна задача...).
- Английский оригинал: "Winnipeg showed interest but the deal is not easy to work out..."
  ❌ Плохой перевод: "...однако сработать сделка не так просто" (Ошибка: корявый синтаксис. Правильно: ...однако провернуть эту сделку будет непросто).

4. Авторство: Формат первой строки строго такой: [Имя Автора по-английски]: [Текст поста]. Пример: Chris Johnston: Эдмонтон вовсю ищет...

5. Имена и команды: Склоняй игроков строго сверяясь с этим глоссарием:
{names_glossary}

6. Финал: В самой последней строке (с новой строки) напиши строго: SEARCH_QUERY: [Имя главного игрока на английском] NHL photo.

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
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1
            }
            response = requests.post(url, headers=headers, json=data, timeout=15)
            if response.status_code == 200:
                ai_text = response.json()['choices'][0]['message']['content']
                if ai_text:
                    return ai_text.replace("**", "").replace('"', "").replace("«", "").replace("»", "").strip()
        except Exception as e:
            print(f"⚠️ Сбой сети GitHub Models: {e}")
            time.sleep(1)

    # --- ШАГ 2: google/gemma-4-31b-it:free через OpenRouter (Резервный) ---
    if OPENROUTER_API_KEY:
        try:
            print("🤖 Шаг 2: Пробуем google/gemma-4-31b-it:free через OpenRouter...")
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
            data = {
                "model": "google/gemma-4-31b-it:free",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1
            }
            response = requests.post(url, headers=headers, json=data, timeout=15)
            if response.status_code == 200:
                ai_text = response.json()['choices'][0]['message']['content']
                if ai_text:
                    return ai_text.replace("**", "").replace('"', "").replace("«", "").replace("»", "").strip()
        except Exception as e:
            print(f"⚠️ Сбой сети OpenRouter (Gemma): {e}")
            time.sleep(1)

    # --- ШАГ 3: openai/gpt-oss-120b:free через OpenRouter (Резервный) ---
    if OPENROUTER_API_KEY:
        try:
            print("🤖 Шаг 3: Пробуем gpt-oss-120b через OpenRouter...")
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
            data = {
                "model": "openai/gpt-oss-120b:free",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1
            }
            response = requests.post(url, headers=headers, json=data, timeout=15)
            if response.status_code == 200:
                ai_text = response.json()['choices'][0]['message']['content']
                if ai_text:
                    return ai_text.replace("**", "").replace('"', "").replace("«", "").replace("»", "").strip()
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
            
            # 1. Применяем исправление спортивной грамматики (падежи имен)
            post_text = fix_sports_grammar(post_text)
            
            # 2. Накатываем жесткую Python-фильтрацию против "ястребов", "гендиров" и "Лио"
            post_text = clean_bot_hallucinations(post_text)
            
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
