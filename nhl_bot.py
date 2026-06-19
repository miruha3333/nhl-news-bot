import g4f
import feedparser
import telebot
import os
import time

# Замени на свои реальные данные
TOKEN = os.environ.get('TOKEN') 
CHANNEL_ID = '-1004423088204' # Твой ID канала (например, -100123456789)

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

def translate_tweet(text):
    # Жесткий промпт, исключающий любой флуд от нейросети
    prompt = f"""
    Ты — автоматический хоккейный редактор. Переведи твит о НХЛ на русский язык.
    
    СТРОГИЕ ПРАВИЛА ФОРМАТИРОВАНИЯ ПОСТА:
    1. Формат вывода должен быть строго: Источник (на английском языке): Текст перевода.
       Пример: Chris Johnston: По поводу Golden Knights: в ближайшие две недели они будут одной из главных фигур.
    2. Если в начале оригинального текста указан автор/источник (например, "Chris Johnston:", "Pierre LeBrun:", "David Pagnotta:"), обязательно оставь его имя на английском языке в самом начале поста, затем поставь двоеточие и пробел. Если автора в тексте нет — начни сразу с перевода.
    3. Убери из итогового текста ВСЕ лишние знаки: кавычки (обычные, « », " "), звездочки (**), лишние скобки в конце. Текст должен быть абсолютно чистым.
    4. Все имена и фамилии игроков внутри перевода оставляй в ОРИГИНАЛЕ на английском (Darnell Nurse, Drew Doughty).
    5. Хоккейные термины: Cap hit -> кэпхит, Trade -> обмен, Free agent -> свободный агент.
    6. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО добавлять от себя любые вводные фразы ("Вот перевод...", "Если нужно...") или примечания. Выдай ТОЛЬКО готовый текст поста.
    
    Оригинальный текст: "{text}"
    """
    
    for attempt in range(3):
        try:
            response = g4f.ChatCompletion.create(
                model=g4f.models.gpt_4o, 
                messages=[{"role": "user", "content": prompt}]
            )
            
            if response:
                # Дополнительная страховка: чистим текст на уровне Python, 
                # если нейросеть всё же проигнорировала правила разметки
                clean_text = response.replace("**", "").replace('"', "").replace("«", "").replace("»", "")
                return clean_text.strip()
        except:
            time.sleep(2)
    return None

def main():
    feed = feedparser.parse("https://nitter.net/NHLRumourReport/rss")
    history = get_history()
    
    # Идем от старых к новым
    for entry in reversed(feed.entries[:5]):
        if entry.title not in history:
            translated = translate_tweet(entry.title)
            
            if translated:
                bot.send_message(CHANNEL_ID, translated)
                add_to_history(entry.title)
                time.sleep(1) # Короткая пауза между отправками

if __name__ == "__main__":
    main()
