import g4f
import feedparser
import telebot
import os

# Замени на свои данные
TOKEN = os.environ.get('TOKEN') 
CHANNEL_ID = '-1004423088204' # Твой ID из шага 2

bot = telebot.TeleBot(TOKEN)
HISTORY_FILE = "history.txt"

def get_history():
    if not os.path.exists(HISTORY_FILE): return set()
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f)

def add_to_history(title):
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(title + "\n")

def translate_tweet(text):
    prompt = f"""Переведи твит о НХЛ. Имена/фамилии — строго на английском (Darnell Nurse). 
    Термины: Cap hit -> кэпхит, Trade -> обмен. Удали источник и дату в конце. 
    Текст: "{text}" """
    return g4f.ChatCompletion.create(model=g4f.models.gpt_4o, messages=[{"role": "user", "content": prompt}])

def main():
    feed = feedparser.parse("https://nitter.net/NHLRumourReport/rss")
    history = get_history()
    # Берем новости в обратном порядке
    for entry in reversed(feed.entries[:5]):
        if entry.title not in history:
            translated = translate_tweet(entry.title)
            bot.send_message(CHANNEL_ID, translated)
            add_to_history(entry.title)

if __name__ == "__main__":
    main()
