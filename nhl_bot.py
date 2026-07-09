import feedparser
import telebot
import os
import time
import requests
import difflib
import pymorphy3
import re
from collections import defaultdict
from ddgs import DDGS

TOKEN = os.environ.get('TOKEN') 
CHANNEL_ID = '-1004423088204' 
OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY')
GH_MODELS_TOKEN = os.environ.get('GH_MODELS_TOKEN') or os.environ.get('GITHUB_TOKEN')

bot = telebot.TeleBot(TOKEN)
HISTORY_FILE = "history.txt"
IMAGE_HISTORY_FILE = "image_history.txt"
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

def get_image_history():
    if not os.path.exists(IMAGE_HISTORY_FILE): 
        return set()
    with open(IMAGE_HISTORY_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f)

def add_to_image_history(url):
    with open(IMAGE_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(url + "\n")

def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def is_duplicate(new_text, existing_texts):
    for text in existing_texts:
        similarity = difflib.SequenceMatcher(None, new_text, text).ratio()
        if similarity > 0.8:
            return True
    return False

def clean_bot_hallucinations(text):
    """Жесткий фильтр терминологии, прозвищ, кривых склонений и вежливых фраз ИИ."""
    if not text: return text
    
    chat_triggers = ["я понял задачу", "понял задачу", "давайте ваш текст", "вот ваш перевод", "конечно, вот", "адаптированный пост"]
    if any(trigger in text.lower() for trigger in chat_triggers):
        print("⚠️ Обнаружен пустой диалог нейросети вместо новости. Блокируем.")
        return ""

    replacements = {
        "анахаймы": "Анахайма",
        "у анахаймы": "у Анахайма",
        "гендир": "генеральный менеджер",
        "дженерал менеджер": "генеральный менеджер",
        "дженерал": "генеральный менеджер",
        "чигагских ястребов": "Чикаго",
        "чикагские ястыебы": "Чикаго",
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
    """Ищет только качественные широкоформатные фото, полностью исключая ИИ-арт."""
    clean_query = f"{query} -getty -alamy -shutterstock -stock -watermark -ai -generated -midjourney -dalle -art -render -drawing -illustration"
    time.sleep(2)
    
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    bad_url_words = ['alamy', 'getty', 'shutterstock', 'depositphotos', 'stock', 'dreamstime', 'vector', 'illustration']
    image_history = get_image_history()
    
    try:
        with DDGS() as ddgs:
            results = list(ddgs.images(
                query=clean_query, 
                max_results=30,
                layout="Wide",
                size="Large"
            ))
            
            for res in results:
                img_url = res['image']
                if img_url in image_history: continue
                if any(bad in img_url.lower() for bad in bad_url_words): continue
                    
                try:
                    response = requests.get(img_url, headers=headers, timeout=10)
                    if response.status_code == 200:
                        add_to_image_history(img_url)
                        with open("temp.png", 'wb') as handler: 
                            handler.write(response.content)
                        return "temp.png"
                except Exception: continue
    except Exception as e:
        print(f"Ошибка поиска картинок: {e}")
    return None

def translate_tweet(raw_text):
    if not raw_text or not raw_text.strip():
        return None
        
    clean_text_for_ai = raw_text.rsplit(' - ', 1)[0] if ' - ' in raw_text else raw_text
    
    if not clean_text_for_ai.strip() or len(clean_text_for_ai.strip()) < 10:
        print(f"⚠️ Новость слишком короткая или пустая ('{clean_text_for_ai}'), отмена запроса к ИИ.")
        return None

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
   - СТРОГО ЗАПРЕЩЕНО отвечать на этот промпт сообщениями в духе "Я понял задачу", "Давайте текст" или "Вот ваш перевод". Твой ответ обязан содержать ТОЛЬКО готовый новостной пост и ничего больше!
   - Если оригинальный инсайд короткий — твой пост должен быть таким же коротким. Никакой лишней «воды».

2. ПРАВИЛО ИМЕН И КОМАНД (ЖЕСТКОЕ ОГРАНИЧЕНИЕ):
   - НАЗВАНИЯ КЛУБОВ: Всегда используй официальные полные названия команд или городов (Чикаго, Эдмонтон, Торонто, Анахайм, Каролина и т.д.) только в ИМЕНИТЕЛЬНОМ ПАДЕЖЕ (отвечая на вопрос "Кто/Что?"). КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО склонять названия клубов (никаких "Эдмонтона", "у Анахаймы", "в Каролине").
   - АДАПТАЦИЯ: Ты обязан полностью перестроить предложение так, чтобы оно звучало естественно с названием клуба в именительном падеже. Смысл должен остаться прежним.
   - ПРИМЕРЫ АДАПТАЦИИ (КАК НАДО ДЕЛАТЬ):
     ✅ Английский: "The decision depends on Chicago."
     ❌ Плохой перевод: "Решение зависит от Чикаго [род. падеж]".
     ✅ Правильная адаптация: "Чикаго [им. падеж] будет принимать окончательное решение."
     
     ✅ Английский: "Anaheim is ready to include Frank Vatrano..."
     ❌ Плохой перевод: "У Анахаймы готовы включить..." (БРЕД).
     ✅ Правильная адаптация: "В клубе Анахайм [им. падеж] готовы включить в сделку Франка Ватрано." или "Анахайм [им. падеж] не против отдать Франка Ватрано..."
     
   - ИМЕНА ИГРОКОВ: Склоняй игроков строго сверяясь с глоссарием ниже. Вне глоссария, если сомневаешься в склонении — оставляй имя в именительном падеже, но не коверкай.
   - ПРОЗВИЩА: КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать прозвища команд (никаких "ястребов", "листьев", "нефтяников").
   - АББРЕВИАТУРЫ: GM -> генеральный менеджер (НИКОГДА не пиши "гендир" или "дженерал"). HC -> главный тренер. NTC/NMC -> пункт о запрете на обмен.

3. ПРАВИЛО СИНТАКСИСА: Перевод должен быть естественным для русского языка. Не копируй английскую структуру фраз. Избегай ломаных конструкций.

4. Авторство: Формат первой строки строго такой: [Имя Автора по-английски]: [Текст поста].

5. Глоссарий имен игроков:
{names_glossary}

6. Финал: В самой последней строке напиши строго: SEARCH_QUERY: [Имя главного игрока на английском] NHL match

Оригинал для обработки: "{clean_text_for_ai}"
    """
    
    if GH_MODELS_TOKEN:
        try:
            url = "https://models.inference.ai.azure.com/chat/completions"
            headers = {"Authorization": f"Bearer {GH_MODELS_TOKEN}", "Content-Type": "application/json"}
            data = {"model": "gpt-4o", "messages": [{"role": "user", "content": prompt}], "temperature": 0.1}
            response = requests.post(url, headers=headers, json=data, timeout=15)
            if response.status_code == 200:
                ai_text = response.json()['choices'][0]['message']['content']
                if ai_text: return ai_text.replace("**", "").replace('"', "").replace("«", "").replace("»", "").strip()
        except Exception: pass

    if OPENROUTER_API_KEY:
        try:
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
            data = {"model": "google/gemma-4-31b-it:free", "messages": [{"role": "user", "content": prompt}], "temperature": 0.1}
            response = requests.post(url, headers=headers, json=data, timeout=15)
            if response.status_code == 200:
                ai_text = response.json()['choices'][0]['message']['content']
                if ai_text: return ai_text.replace("**", "").replace('"', "").replace("«", "").replace("»", "").strip()
        except Exception: pass

    return None

def alternate_posts(posts):
    """Алгоритм чередования: группирует новости по игрокам и перемешивает их."""
    if not posts: return []
    grouped = defaultdict(list)
    for p in posts:
        grouped[p['query']].append(p)
    
    sorted_posts = []
    while any(grouped.values()):
        for key in list(grouped.keys()):
            if grouped[key]:
                sorted_posts.append(grouped[key].pop(0))
            else:
                del grouped[key]
    return sorted_posts

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
        
    raw_posts_pool = []
    entries_to_save = []
    
    for entry in new_entries:
        raw_response = translate_tweet(entry.title)
        if raw_response:
            query_part = "NHL match action"
            post_text = raw_response
            if "SEARCH_QUERY:" in raw_response:
                idx = raw_response.rfind("SEARCH_QUERY:")
                post_text = raw_response[:idx].strip()
                query_part = raw_response[idx:].replace("SEARCH_QUERY:", "").strip()
            
            post_text = fix_sports_grammar(post_text)
            post_text = clean_bot_hallucinations(post_text)
            
            if not post_text.strip():
                entries_to_save.append(entry.title)
                continue
            
            if ": " in post_text:
                author, text_content = post_text.split(": ", 1)
                raw_posts_pool.append({
                    'author': author.replace("Источник", "").strip(),
                    'text': text_content,
                    'query': query_part,
                    'entry_title': entry.title
                })
            else:
                raw_posts_pool.append({
                    'author': "",
                    'text': post_text,
                    'query': query_part,
                    'entry_title': entry.title
                })

    alternated_posts = alternate_posts(raw_posts_pool)
    
    combined_texts = []
    pure_texts_for_diff = []
    search_queries = []
    
    for p in alternated_posts:
        if is_duplicate(p['text'], pure_texts_for_diff):
            entries_to_save.append(p['entry_title'])
            continue
            
        pure_texts_for_diff.append(p['text'])
        if p['author']:
            formatted_text = f"<b>{escape_html(p['author'])}</b>\n{escape_html(p['text'])}"
        else:
            formatted_text = escape_html(p['text'])
            
        combined_texts.append(formatted_text)
        search_queries.append(p['query'])
        entries_to_save.append(p['entry_title'])
            
    if combined_texts:
        final_post = "\n\n".join(combined_texts)
        image_path = None
        
        for query in search_queries:
            image_path = download_image(query)
            if image_path: break
                
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
                    if os.path.exists(image_path): os.remove(image_path)
            
            if not image_sent:
                bot.send_message(CHANNEL_ID, final_post, parse_mode='HTML')
            
            for title in entries_to_save:
                add_to_history(title)
                
        except Exception as e:
            print(f"❌ Ошибка отправки: {e}")

if __name__ == "__main__":
    main()
