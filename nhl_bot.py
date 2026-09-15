import os
import re
import time
import sqlite3
import hashlib
from datetime import datetime
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from ddgs import DDGS

# =============================================================================

# SETTINGS

# =============================================================================

RSS_URL = os.getenv("RSS_URL", "").strip()

TELEGRAM_TOKEN = os.getenv("TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID", "").strip()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

DATABASE_FILE = "nhl_bot.db"

MAX_RSS_ENTRIES = 30

GEMINI_PRIMARY_MODEL = "gemini-3.5-flash"
GEMINI_FALLBACK_MODEL = "gemini-3.1-flash-lite"

GEMINI_TIMEOUT = 90

TELEGRAM_TIMEOUT = 60

IMAGE_RESULTS_LIMIT = 30

IMAGE_DOWNLOAD_TIMEOUT = 20

GEMINI_DELAY = 1.0

MAX_ARTICLE_TEXT = 12000

# Image scoring settings.

FRESH_DAYS = 90
HISTORICAL_YEAR_TOLERANCE = 3

# =============================================================================

# IMAGE SCORING CONFIG

# =============================================================================

GOOD_DOMAINS = {
"nhl.com": 70,
"espn.com": 65,
"sportsnet.ca": 60,
"tsn.ca": 60,
"reuters.com": 50,
"apnews.com": 50,
"usatoday.com": 45,
"theathletic.com": 45,
"cbc.ca": 40,
"si.com": 40,
"detroitnews.com": 40,
"freep.com": 40,
}

BAD_DOMAINS = {
"gettyimages.com": -100,
"alamy.com": -100,
"shutterstock.com": -100,
"depositphotos.com": -100,
"dreamstime.com": -100,
"istockphoto.com": -100,
"123rf.com": -100,
"stock.adobe.com": -100,
}

BAD_WORDS = {
"logo": -100,
"infographic": -100,
"illustration": -80,
"wallpaper": -70,
"poster": -70,
"merchandise": -100,
"shirt": -80,
"jersey sale": -80,
"basketball": -120,
"football": -120,
"baseball": -120,
"soccer": -120,
"golf": -100,
"wrestling": -100,
"podcast": -30,
}

GOOD_WORDS = {
"nhl": 10,
"hockey": 10,
"ice hockey": 10,
}

OPPONENT_PATTERNS = [
r"\bvs.?\b",
r"\bversus\b",
r"\bagainst\b",
r"\bface\b",
r"\bfacing\b",
r"\bfaces\b",
]

TEAM_CHANGE_WORDS = [
"sign",
"signed",
"signing",
"contract",
"joins",
"joined",
"join",
"acquired",
"traded",
"trade",
"deal",
"agrees",
"agreed",
"lands",
"new home",
]

# =============================================================================

# GEMINI STATE

# =============================================================================

gemini_primary_disabled = False

# =============================================================================

# DATABASE

# =============================================================================

def get_existing_columns(connection, table_name):
"""
Return the existing column names for a SQLite table.

```
This is used to migrate databases created by older versions
of the bot without deleting existing data.
"""
cursor = connection.execute(
    f"PRAGMA table_info({table_name})"
)

return {
    row[1]
    for row in cursor.fetchall()
}
```

def add_column_if_missing(
connection,
table_name,
column_name,
column_definition
):
"""
Add a column only if it does not already exist.

```
SQLite supports ALTER TABLE ... ADD COLUMN, which is enough
for the schema changes used by this bot.
"""
columns = get_existing_columns(
    connection,
    table_name
)

if column_name in columns:
    return False

print(
    f"[DATABASE] Adding missing column "
    f"{table_name}.{column_name}"
)

connection.execute(
    f"ALTER TABLE {table_name} "
    f"ADD COLUMN {column_name} "
    f"{column_definition}"
)

return True
```

def migrate_database(connection):
"""
Migrate databases created by older versions of the bot.

```
Existing rows are preserved.

The current bot expects:

    news:
        url
        title
        processed
        created_at

    images:
        url
        used
        created_at

    errors:
        url
        error
        created_at
"""

print(
    "[DATABASE] Checking database schema..."
)

# -------------------------------------------------------------------------
# NEWS
# -------------------------------------------------------------------------

connection.execute(
    """
    CREATE TABLE IF NOT EXISTS news (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT UNIQUE NOT NULL,
        title TEXT,
        processed INTEGER DEFAULT 0,
        created_at TEXT
    )
    """
)

add_column_if_missing(
    connection,
    "news",
    "title",
    "TEXT"
)

add_column_if_missing(
    connection,
    "news",
    "processed",
    "INTEGER DEFAULT 0"
)

add_column_if_missing(
    connection,
    "news",
    "created_at",
    "TEXT"
)

# -------------------------------------------------------------------------
# IMAGES
# -------------------------------------------------------------------------

connection.execute(
    """
    CREATE TABLE IF NOT EXISTS images (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT UNIQUE NOT NULL,
        used INTEGER DEFAULT 0,
        created_at TEXT
    )
    """
)

add_column_if_missing(
    connection,
    "images",
    "used",
    "INTEGER DEFAULT 0"
)

add_column_if_missing(
    connection,
    "images",
    "created_at",
    "TEXT"
)

# -------------------------------------------------------------------------
# ERRORS
# -------------------------------------------------------------------------

connection.execute(
    """
    CREATE TABLE IF NOT EXISTS errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT,
        error TEXT,
        created_at TEXT
    )
    """
)

add_column_if_missing(
    connection,
    "errors",
    "url",
    "TEXT"
)

add_column_if_missing(
    connection,
    "errors",
    "error",
    "TEXT"
)

add_column_if_missing(
    connection,
    "errors",
    "created_at",
    "TEXT"
)

connection.commit()

print(
    "[DATABASE] Schema check complete."
)
```

def get_db():
connection = sqlite3.connect(
DATABASE_FILE,
timeout=30
)

```
migrate_database(
    connection
)

return connection
```

# =============================================================================

# DATABASE HELPERS

# =============================================================================

def normalize_url(url):
if not url:
return ""

```
url = str(url).strip()

if "#" in url:
    url = url.split("#", 1)[0]

url = url.replace(
    "https://www.",
    "https://"
)

url = url.replace(
    "http://www.",
    "http://"
)

return url.rstrip("/")
```

def is_news_processed(url):
url = normalize_url(url)

```
if not url:
    return True

connection = get_db()

try:
    cursor = connection.execute(
        """
        SELECT processed
        FROM news
        WHERE url = ?
        """,
        (url,)
    )

    row = cursor.fetchone()

    if not row:
        return False

    return bool(row[0])

finally:
    connection.close()
```

def save_news(url, title="", processed=False):
url = normalize_url(url)

```
if not url:
    return

connection = get_db()

try:
    connection.execute(
        """
        INSERT INTO news (
            url,
            title,
            processed,
            created_at
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(url)
        DO UPDATE SET
            title = excluded.title,
            processed = excluded.processed
        """,
        (
            url,
            title,
            1 if processed else 0,
            datetime.utcnow().isoformat()
        )
    )

    connection.commit()

finally:
    connection.close()
```

def mark_news_processed(url):
url = normalize_url(url)

```
if not url:
    return

connection = get_db()

try:
    connection.execute(
        """
        UPDATE news
        SET processed = 1
        WHERE url = ?
        """,
        (url,)
    )

    connection.commit()

finally:
    connection.close()
```

def is_image_used(url):
if not url:
return False

```
connection = get_db()

try:
    cursor = connection.execute(
        """
        SELECT used
        FROM images
        WHERE url = ?
        """,
        (url,)
    )

    row = cursor.fetchone()

    if not row:
        return False

    return bool(row[0])

finally:
    connection.close()
```

def save_image(url, used=False):
if not url:
return

```
connection = get_db()

try:
    connection.execute(
        """
        INSERT INTO images (
            url,
            used,
            created_at
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(url)
        DO UPDATE SET
            used = excluded.used
        """,
        (
            url,
            1 if used else 0,
            datetime.utcnow().isoformat()
        )
    )

    connection.commit()

finally:
    connection.close()
```

def mark_image_used(url):
if not url:
return

```
connection = get_db()

try:
    connection.execute(
        """
        UPDATE images
        SET used = 1
        WHERE url = ?
        """,
        (url,)
    )

    connection.commit()

finally:
    connection.close()
```

def log_error(url, error):
connection = get_db()

```
try:
    connection.execute(
        """
        INSERT INTO errors (
            url,
            error,
            created_at
        )
        VALUES (?, ?, ?)
        """,
        (
            normalize_url(url),
            str(error)[:4000],
            datetime.utcnow().isoformat()
        )
    )

    connection.commit()

finally:
    connection.close()
```

# =============================================================================

# RSS

# =============================================================================

def clean_html(text):
if not text:
return ""

```
soup = BeautifulSoup(
    str(text),
    "html.parser"
)

return soup.get_text(
    " ",
    strip=True
)
```

def get_rss_entries():
if not RSS_URL:
raise RuntimeError(
"RSS_URL is not configured"
)

```
print("[RSS] Loading feed...")

feed = feedparser.parse(
    RSS_URL
)

print(
    f"[RSS] Entries received: "
    f"{len(feed.entries)}"
)

entries = []

for entry in feed.entries[:MAX_RSS_ENTRIES]:

    title = clean_html(
        getattr(
            entry,
            "title",
            ""
        )
    )

    link = normalize_url(
        getattr(
            entry,
            "link",
            ""
        )
    )

    summary = clean_html(
        getattr(
            entry,
            "summary",
            ""
        )
    )

    if not link:
        continue

    entries.append(
        {
            "title": title,
            "link": link,
            "summary": summary
        }
    )

return entries
```

# =============================================================================

# ARTICLE TEXT

# =============================================================================

def get_article_text(entry):
parts = []

```
title = entry.get(
    "title",
    ""
)

summary = entry.get(
    "summary",
    ""
)

if title:
    parts.append(
        f"TITLE:\n{title}"
    )

if summary:
    parts.append(
        f"SUMMARY:\n{summary}"
    )

text = "\n\n".join(parts)

return text[:MAX_ARTICLE_TEXT]
```

# =============================================================================

# GEMINI

# =============================================================================

def gemini_request(
prompt,
model
):
url = (
"https://generativelanguage.googleapis.com/"
"v1beta/models/"
f"{model}:generateContent"
)

```
params = {
    "key": GEMINI_API_KEY
}

payload = {
    "contents": [
        {
            "parts": [
                {
                    "text": prompt
                }
            ]
        }
    ]
}

response = requests.post(
    url,
    params=params,
    json=payload,
    timeout=GEMINI_TIMEOUT
)

if response.status_code != 200:
    raise RuntimeError(
        f"Gemini HTTP "
        f"{response.status_code}: "
        f"{response.text[:1000]}"
    )

data = response.json()

candidates = data.get(
    "candidates",
    []
)

if not candidates:
    raise RuntimeError(
        "Gemini returned no candidates"
    )

content = candidates[0].get(
    "content",
    {}
)

parts = content.get(
    "parts",
    []
)

texts = []

for part in parts:
    text = part.get(
        "text",
        ""
    )

    if text:
        texts.append(
            text
        )

result = "\n".join(
    texts
).strip()

if not result:
    raise RuntimeError(
        "Gemini returned empty text"
    )

return result
```

def clean_gemini_response(text):
if not text:
return ""

````
text = str(text).strip()

text = re.sub(
    r"```(?:text|json)?",
    "",
    text,
    flags=re.IGNORECASE
)

text = text.replace(
    "```",
    ""
)

return text.strip()
````

def extract_field(
text,
field
):
pattern = (
rf"{re.escape(field)}\s*:"
rf"\s*(.+)"
)

```
match = re.search(
    pattern,
    text,
    flags=re.IGNORECASE
)

if not match:
    return ""

value = match.group(1).strip()

value = value.strip(
    "\"'[]"
)

return value.strip()
```

def build_fallback_search_query(
title,
summary
):
text = " ".join(
[
title or "",
summary or ""
]
)

```
text = clean_html(
    text
)

text = re.sub(
    r"\s+",
    " ",
    text
).strip()

if not text:
    return "NHL hockey"

words = text.split()

result = []

stopwords = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "but",
    "for",
    "with",
    "from",
    "into",
    "after",
    "before",
    "about",
    "this",
    "that",
    "have",
    "has",
    "had",
    "will",
    "would",
    "could",
    "should",
    "their",
    "they",
    "them",
    "his",
    "her",
    "its",
    "are",
    "was",
    "were",
    "been",
    "being",
    "is",
    "to",
    "of",
    "in",
    "on",
    "at",
    "by",
    "as",
    "it",
    "he",
    "she",
    "who",
    "what",
    "why",
    "how",
    "when",
    "where",
    "which",
    "more",
    "most",
    "some",
    "any",
    "not",
    "no"
}

for word in words:

    clean = re.sub(
        r"[^A-Za-z0-9'-]",
        "",
        word
    )

    if not clean:
        continue

    if clean.lower() in stopwords:
        continue

    result.append(
        clean
    )

    if len(result) >= 8:
        break

if "NHL" not in result:
    result.append(
        "NHL"
    )

return " ".join(
    result[:10]
)
```

def translate_tweet(
title,
article_text
):
global gemini_primary_disabled

```
if not GEMINI_API_KEY:
    raise RuntimeError(
        "GEMINI_API_KEY is not configured"
    )

prompt = f"""
```

Ты работаешь с новостью NHL.

Твоя задача:

1. Написать короткий естественный пост
   на русском языке для Telegram-канала.
2. Не переводить исходный текст дословно.
3. Сохранить факты исходной новости.
4. Не придумывать информацию.
5. Пост должен звучать как текст живого
   автора спортивного Telegram-канала.
6. Не использовать формальный журналистский
   стиль и канцелярит.
7. Не добавлять информацию, которой нет
   в исходном материале.
8. Отдельно создать поисковый запрос
   для фотографии.

ВАЖНО:

SEARCH_QUERY должен описывать СМЫСЛ
конкретной новости.

Не делай запрос просто в формате:
"Имя игрока NHL".

Если новость о переходе, контракте,
обмене, конфликте, новом клубе, увольнении,
травме, слухе или другом событии —
это событие должно попасть в поисковый запрос.

Запрос должен быть на английском языке.

Верни результат строго в формате:

POST:
текст поста

SEARCH_QUERY:
поисковый запрос

TITLE:
{title}

ARTICLE:
{article_text}
"""

```
models = []

if not gemini_primary_disabled:
    models.append(
        GEMINI_PRIMARY_MODEL
    )

models.append(
    GEMINI_FALLBACK_MODEL
)

last_error = None

for index, model in enumerate(
    models
):

    try:

        print(
            f"[GEMINI] Model: {model}"
        )

        result = gemini_request(
            prompt,
            model
        )

        result = clean_gemini_response(
            result
        )

        post = extract_field(
            result,
            "POST"
        )

        search_query = extract_field(
            result,
            "SEARCH_QUERY"
        )

        if not post:
            raise RuntimeError(
                "Gemini did not return POST"
            )

        if not search_query:
            search_query = (
                build_fallback_search_query(
                    title,
                    article_text
                )
            )

            print(
                "[GEMINI] SEARCH_QUERY "
                "missing, using fallback"
            )

        print(
            f"[GEMINI] Success: "
            f"{model}"
        )

        return {
            "post": post,
            "search_query": search_query
        }

    except Exception as error:

        last_error = error

        print(
            f"[GEMINI ERROR] "
            f"{model}: {error}"
        )

        error_text = str(
            error
        ).lower()

        if (
            "429" in error_text
            or "quota" in error_text
            or "resource_exhausted"
            in error_text
        ):
            if model == GEMINI_PRIMARY_MODEL:
                gemini_primary_disabled = True

                print(
                    "[GEMINI] Primary model "
                    "disabled for the rest "
                    "of this run because "
                    "of quota/rate limit."
                )

        if index < len(models) - 1:
            print(
                "[GEMINI] Switching "
                "to fallback model..."
            )

            time.sleep(
                1
            )

print(
    "[GEMINI] All models failed."
)

fallback_query = (
    build_fallback_search_query(
        title,
        article_text
    )
)

if last_error:
    print(
        "[GEMINI] Emergency fallback "
        "will be used."
    )

return {
    "post": article_text[:4000],
    "search_query": fallback_query
}
```

# =============================================================================

# IMAGE SEARCH

# =============================================================================

BAD_URL_PATTERNS = [
"getty",
"alamy",
"shutterstock",
"depositphotos",
"dreamstime",
"istockphoto",
"stockphoto",
"stock",
"vector",
"illustration",
"wallpaper",
"wallpapers",
"pinterest",
"facebook",
"instagram",
"twitter",
"x.com",
"logo",
"icon",
"avatar",
"thumbnail",
"sprite",
"favicon",
"default",
"placeholder"
]

BAD_DOMAIN_PATTERNS = [
"gettyimages",
"alamy",
"shutterstock",
"depositphotos",
"dreamstime",
"istockphoto",
"pinterest",
"facebook",
"instagram",
"twitter"
]

def normalize_text(value):
if not value:
return ""

```
value = str(value).lower()
value = value.replace(
    "’",
    "'"
)

value = re.sub(
    r"\s+",
    " ",
    value
)

return value.strip()
```

def get_domain(url):
if not url:
return ""

```
try:
    domain = urlparse(
        url
    ).netloc.lower()

    domain = domain.replace(
        "www.",
        ""
    )

    return domain

except Exception:
    return ""
```

def get_base_domain(domain):
parts = domain.split(".")

```
if len(parts) >= 2:
    return ".".join(
        parts[-2:]
    )

return domain
```

def extract_years(text):
if not text:
return []

```
years = []

for match in re.findall(
    r"\b(19\d{2}|20\d{2})\b",
    str(text)
):
    years.append(
        int(match)
    )

return years
```

def to_int(value):
if value is None:
return None

```
try:
    return int(value)

except (
    TypeError,
    ValueError
):
    return None
```

def parse_result_date(result):
possible_fields = [
"date",
"published",
"published_date",
"datetime",
"timestamp",
"title",
"body",
"snippet",
"source",
"url",
]

```
for field in possible_fields:

    value = result.get(
        field
    )

    if not value:
        continue

    years = extract_years(
        value
    )

    for year in years:

        if (
            1900
            <= year
            <= datetime.now().year + 1
        ):
            return year

return None
```

def is_probably_valid_result(result):
image_url = result.get(
"image"
)

```
if not image_url:
    return False

width = to_int(
    result.get("width")
)

height = to_int(
    result.get("height")
)

if width and width < 300:
    return False

if height and height < 200:
    return False

return True
```

def normalize_page_url(url):
if not url:
return ""

```
try:
    parsed = urlparse(
        url
    )

    path = parsed.path.rstrip(
        "/"
    )

    return (
        f"{parsed.scheme.lower()}://"
        f"{parsed.netloc.lower()}"
        f"{path}"
    )

except Exception:
    return url
```

def make_result_key(result):
page_url = normalize_page_url(
result.get("url")
)

```
if page_url:
    return page_url

image_url = result.get(
    "image",
    ""
)

return image_url.split(
    "?",
    1
)[0]
```

def deduplicate_results(results):
unique = {}

```
for result in results:

    key = make_result_key(
        result
    )

    if not key:
        continue

    if key not in unique:
        unique[key] = result
        continue

    old = unique[key]

    old_width = (
        to_int(
            old.get("width")
        )
        or 0
    )

    old_height = (
        to_int(
            old.get("height")
        )
        or 0
    )

    new_width = (
        to_int(
            result.get("width")
        )
        or 0
    )

    new_height = (
        to_int(
            result.get("height")
        )
        or 0
    )

    if (
        new_width * new_height
        > old_width * old_height
    ):
        unique[key] = result

return list(
    unique.values()
)
```

def image_is_bad(
item
):
image_url = str(
item.get(
"image",
""
)
).lower()

```
thumbnail_url = str(
    item.get(
        "thumbnail",
        ""
    )
).lower()

page_url = str(
    item.get(
        "url",
        ""
    )
).lower()

title = str(
    item.get(
        "title",
        ""
    )
).lower()

source = str(
    item.get(
        "source",
        ""
    )
).lower()

combined = " ".join(
    [
        image_url,
        thumbnail_url,
        page_url,
        title,
        source
    ]
)

for pattern in BAD_URL_PATTERNS:

    if pattern in combined:
        return True

try:

    domain = urlparse(
        page_url
    ).netloc.lower()

    for pattern in BAD_DOMAIN_PATTERNS:

        if pattern in domain:
            return True

except Exception:
    pass

return False
```

def score_query_match(
text,
query
):
score = 0
reasons = []

```
text = normalize_text(
    text
)

query_words = [
    word
    for word in normalize_text(
        query
    ).split()
    if len(word) >= 3
]

matched = 0

for word in query_words:

    if word in text:
        matched += 1

if query_words:

    if matched == len(
        query_words
    ):

        score += 25

        reasons.append(
            f"query_match:all "
            f"{matched}/{len(query_words)}"
        )

    elif (
        matched / len(query_words)
        >= 0.75
    ):

        score += 18

        reasons.append(
            f"query_match:high "
            f"{matched}/{len(query_words)}"
        )

    elif (
        matched / len(query_words)
        >= 0.5
    ):

        score += 10

        reasons.append(
            f"query_match:medium "
            f"{matched}/{len(query_words)}"
        )

    else:

        score -= 10

        reasons.append(
            f"query_match:low "
            f"{matched}/{len(query_words)}"
        )

return score, reasons
```

def score_source(domain):
domain = get_base_domain(
domain
)

```
if domain in GOOD_DOMAINS:

    return (
        GOOD_DOMAINS[domain],
        f"source:{domain}"
    )

if domain in BAD_DOMAINS:

    return (
        BAD_DOMAINS[domain],
        f"source:{domain}"
    )

return 0, None
```

def score_bad_words(text):
score = 0
reasons = []

```
normalized = normalize_text(
    text
)

for word, penalty in BAD_WORDS.items():

    if word in normalized:

        score += penalty

        reasons.append(
            f"bad:{word}"
        )

return score, reasons
```

def score_good_words(text):
score = 0
reasons = []

```
normalized = normalize_text(
    text
)

for word, bonus in GOOD_WORDS.items():

    if word in normalized:

        score += bonus

        reasons.append(
            f"good:{word}"
        )

return score, reasons
```

def score_dimensions(
width,
height
):
score = 0
reasons = []

```
if width:

    if width >= 1200:

        score += 10

        reasons.append(
            f"width:{width}"
        )

    elif width >= 800:

        score += 6

        reasons.append(
            f"width:{width}"
        )

    elif width >= 600:

        score += 3

        reasons.append(
            f"width:{width}"
        )

if height:

    if height >= 700:

        score += 10

        reasons.append(
            f"height:{height}"
        )

    elif height >= 500:

        score += 6

        reasons.append(
            f"height:{height}"
        )

    elif height >= 400:

        score += 3

        reasons.append(
            f"height:{height}"
        )

if width and height:

    ratio = width / height

    if 1.3 <= ratio <= 2.0:

        score += 10

        reasons.append(
            f"ratio:{ratio:.2f}"
        )

    elif 1.15 <= ratio <= 2.2:

        score += 5

        reasons.append(
            f"ratio:{ratio:.2f}"
        )

    elif ratio < 0.8:

        score -= 10

        reasons.append(
            f"portrait_ratio:{ratio:.2f}"
        )

return score, reasons
```

def title_has_team_change_context(
title
):
normalized = normalize_text(
title
)

```
for word in TEAM_CHANGE_WORDS:

    if word in normalized:
        return True

return False
```

def get_query_phrases(query):
"""
Build meaningful multi-word phrases from the image search query.

```
The longest phrases are useful for identifying a team/entity mentioned
in the query without requiring a hardcoded player/team database.
"""
words = re.findall(
    r"[A-Za-z0-9'-]+",
    normalize_text(query)
)

if not words:
    return []

phrases = []

for size in (4, 3, 2):

    for index in range(
        len(words) - size + 1
    ):

        phrase = " ".join(
            words[
                index:index + size
            ]
        )

        if phrase not in phrases:
            phrases.append(
                phrase
            )

return phrases
```

def is_phrase_opponent_in_title(
title,
phrase
):
normalized_title = normalize_text(
title
)

```
normalized_phrase = normalize_text(
    phrase
)

if (
    not normalized_title
    or not normalized_phrase
):
    return False

position = normalized_title.find(
    normalized_phrase
)

if position == -1:
    return False

start = max(
    0,
    position - 80
)

end = min(
    len(normalized_title),
    position
    + len(normalized_phrase)
    \+ 30
)

context = normalized_title[
    start:end
]

for pattern in OPPONENT_PATTERNS:

    if re.search(
        pattern,
        context
    ):
        return True

return False
```

def score_title_context(
title,
query,
historical_year=None
):
score = 0
reasons = []

```
normalized = normalize_text(
    title
)

if not normalized:
    return score, reasons

query_normalized = normalize_text(
    query
)

query_words = [
    word
    for word in query_normalized.split()
    if len(word) >= 3
]

phrases = get_query_phrases(
    query
)

opponent_found = False

for phrase in phrases:

    if len(
        phrase.split()
    ) < 2:
        continue

    if phrase in normalized:

        if is_phrase_opponent_in_title(
            title,
            phrase
        ):

            score -= 35

            reasons.append(
                f"title:query_phrase:"
                f"opponent:{phrase}"
            )

            opponent_found = True

            break

matched = 0

for word in query_words:

    if word in normalized:
        matched += 1

if query_words:

    if matched == len(
        query_words
    ):

        score += 20

        reasons.append(
            f"title:query_match:"
            f"all {matched}/{len(query_words)}"
        )

    elif (
        matched / len(query_words)
        >= 0.75
    ):

        score += 12

        reasons.append(
            f"title:query_match:"
            f"high {matched}/{len(query_words)}"
        )

    elif (
        matched / len(query_words)
        >= 0.5
    ):

        score += 6

        reasons.append(
            f"title:query_match:"
            f"medium {matched}/{len(query_words)}"
        )

if title_has_team_change_context(
    title
):

    if not opponent_found:

        score += 25

        reasons.append(
            "title:team_change"
        )

if historical_year:

    if str(
        historical_year
    ) in normalized:

        score += 60

        reasons.append(
            f"title:historical_year:"
            f"{historical_year}"
        )

    historical_words = [
        "goal",
        "scored",
        "scoring",
        "hat trick",
        "historic",
        "history",
        "classic",
        "legendary",
        "highlights",
        "throwback",
        "retro",
    ]

    for word in historical_words:

        if word in normalized:

            score += 8

            reasons.append(
                f"title:event:{word}"
            )

else:

    current_words = [
        "2026",
        "2025",
        "2024",
        "signing",
        "signed",
        "contract",
        "trade",
        "traded",
        "acquired",
        "joins",
        "joined",
        "debut",
    ]

    for word in current_words:

        if word in normalized:

            score += 4

            reasons.append(
                f"title:current:{word}"
            )

return score, reasons
```

def score_date(
result,
historical_year=None
):
score = 0
reasons = []

```
result_year = parse_result_date(
    result
)

if result_year is None:

    possible_text = " ".join(
        [
            str(
                result.get("title")
                or ""
            ),
            str(
                result.get("body")
                or ""
            ),
            str(
                result.get("snippet")
                or ""
            ),
            str(
                result.get("url")
                or ""
            ),
        ]
    )

    years = extract_years(
        possible_text
    )

    if years:

        result_year = max(
            years
        )

if not result_year:

    return 0, [
        "date:unknown"
    ]

current_year = datetime.now().year

if historical_year:

    difference = abs(
        result_year
        - historical_year
    )

    if difference == 0:

        score += 80

        reasons.append(
            f"historical_date:"
            f"exact:{result_year}"
        )

    elif difference == 1:

        score += 55

        reasons.append(
            f"historical_date:"
            f"+-1:{result_year}"
        )

    elif (
        difference
        <= HISTORICAL_YEAR_TOLERANCE
    ):

        score += 30

        reasons.append(
            f"historical_date:"
            f"near:{result_year}"
        )

    elif difference <= 10:

        score += 5

        reasons.append(
            f"historical_date:"
            f"far:{result_year}"
        )

    else:

        score -= 45

        reasons.append(
            f"historical_date:"
            f"mismatch:{result_year}"
        )

    return score, reasons

age_years = (
    current_year
    - result_year
)

if age_years <= 0:

    score += 30

    reasons.append(
        f"date:current:{result_year}"
    )

elif age_years == 1:

    score += 20

    reasons.append(
        f"date:recent:{result_year}"
    )

elif age_years == 2:

    score += 10

    reasons.append(
        f"date:fairly_recent:"
        f"{result_year}"
    )

elif age_years <= 5:

    reasons.append(
        f"date:older:{result_year}"
    )

else:

    score -= 20

    reasons.append(
        f"date:old:{result_year}"
    )

return score, reasons
```

def score_result(
result,
query,
historical_year=None
):
title = result.get(
"title"
) or ""

```
body = (
    result.get("body")
    or result.get("snippet")
    or ""
)

source = result.get(
    "source"
) or ""

url = result.get(
    "url"
) or ""

image = result.get(
    "image"
) or ""

width = to_int(
    result.get("width")
)

height = to_int(
    result.get("height")
)

domain = get_domain(
    url
)

combined_text = " ".join(
    [
        str(title),
        str(body),
        str(source),
        str(url),
        str(image),
    ]
)

score = 0
reasons = []

points, why = score_query_match(
    combined_text,
    query
)

score += points
reasons.extend(
    why
)

points, why = score_title_context(
    title,
    query,
    historical_year
)

score += points
reasons.extend(
    why
)

points, why = score_source(
    domain
)

score += points

if why:
    reasons.append(
        why
    )

points, why = score_bad_words(
    combined_text
)

score += points
reasons.extend(
    why
)

points, why = score_good_words(
    combined_text
)

score += points
reasons.extend(
    why
)

points, why = score_dimensions(
    width,
    height
)

score += points
reasons.extend(
    why
)

points, why = score_date(
    result,
    historical_year
)

score += points
reasons.extend(
    why
)

return score, reasons
```

def download_image(
query,
historical_year=None
):
if not query:
return None

```
print(
    f"[IMAGE] Search query: "
    f"{query}"
)

if historical_year:
    print(
        f"[IMAGE] Historical year: "
        f"{historical_year}"
    )

searches = [
    {
        "timelimit": "m",
        "label": "recent month"
    },
    {
        "timelimit": "y",
        "label": "recent year"
    },
    {
        "timelimit": None,
        "label": "all time"
    }
]

all_results = []

for search in searches:

    try:

        print(
            f"[IMAGE] Search mode: "
            f"{search['label']}"
        )

        kwargs = {
            "query": query,
            "max_results":
                IMAGE_RESULTS_LIMIT,
            "safesearch": "moderate",
            "layout": "Wide",
            "size": "Large"
        }

        if search["timelimit"]:

            kwargs["timelimit"] = (
                search["timelimit"]
            )

        with DDGS() as ddgs:

            results = list(
                ddgs.images(
                    **kwargs
                )
            )

        print(
            f"[IMAGE] Results: "
            f"{len(results)}"
        )

        for item in results:

            item["_search_query"] = (
                query
            )

            all_results.append(
                item
            )

    except Exception as error:

        print(
            "[IMAGE SEARCH ERROR] "
            f"{error}"
        )

        continue

if not all_results:

    print(
        "[IMAGE] No search results."
    )

    return None

print(
    f"[IMAGE] Total raw results: "
    f"{len(all_results)}"
)

unique_results = (
    deduplicate_results(
        all_results
    )
)

print(
    f"[IMAGE] Unique article/image "
    f"groups: {len(unique_results)}"
)

valid_results = []

for result in unique_results:

    if not is_probably_valid_result(
        result
    ):
        continue

    if image_is_bad(
        result
    ):
        continue

    image_url = result.get(
        "image"
    )

    if not image_url:
        continue

    if is_image_used(
        image_url
    ):
        continue

    valid_results.append(
        result
    )

print(
    f"[IMAGE] Valid candidates: "
    f"{len(valid_results)}"
)

if not valid_results:

    print(
        "[IMAGE] No acceptable "
        "candidates."
    )

    return None

scored = []

for result in valid_results:

    result_query = (
        result.get(
            "_search_query",
            query
        )
    )

    score, reasons = score_result(
        result,
        result_query,
        historical_year
    )

    result["_score"] = score
    result["_reasons"] = reasons

    scored.append(
        result
    )

scored.sort(
    key=lambda item: (
        item.get(
            "_score",
            0
        )
    ),
    reverse=True
)

print()
print(
    "[IMAGE] TOP CANDIDATES"
)

for index, result in enumerate(
    scored[:10],
    start=1
):

    print(
        f"[{index}] "
        f"SCORE: "
        f"{result.get('_score', 0)}"
    )

    print(
        "      TITLE: "
        f"{result.get('title', '')}"
    )

    print(
        "      SOURCE: "
        f"{result.get('source', '')}"
    )

    print(
        "      SIZE: "
        f"{to_int(result.get('width'))}x"
        f"{to_int(result.get('height'))}"
    )

    print(
        "      DATE: "
        f"{parse_result_date(result)}"
    )

    print(
        "      PAGE: "
        f"{result.get('url', '')}"
    )

    for reason in result.get(
        "_reasons",
        []
    ):

        print(
            f"      + {reason}"
        )

for result in scored[:10]:

    image_url = result.get(
        "image"
    )

    if not image_url:
        continue

    print()
    print(
        "[IMAGE] Trying candidate:"
    )

    print(
        f"[IMAGE] Title: "
        f"{result.get('title', '')}"
    )

    print(
        f"[IMAGE] Source: "
        f"{result.get('source', '')}"
    )

    print(
        f"[IMAGE] Score: "
        f"{result.get('_score', 0)}"
    )

    try:

        response = requests.get(
            image_url,
            timeout=
            IMAGE_DOWNLOAD_TIMEOUT,
            headers={
                "User-Agent":
                    (
                        "Mozilla/5.0 "
                        "(Windows NT 10.0; "
                        "Win64; x64) "
                        "AppleWebKit/537.36 "
                        "Chrome/124 "
                        "Safari/537.36"
                    )
            }
        )

        if response.status_code != 200:

            print(
                "[IMAGE] HTTP status: "
                f"{response.status_code}"
            )

            continue

        content_type = (
            response.headers
            .get(
                "Content-Type",
                ""
            )
            .lower()
        )

        if "image" not in content_type:

            print(
                "[IMAGE] Not an image: "
                f"{content_type}"
            )

            continue

        content = response.content

        if len(content) < 10_000:

            print(
                "[IMAGE] Image too small."
            )

            continue

        extension = ".jpg"

        if "png" in content_type:
            extension = ".png"

        elif "webp" in content_type:
            extension = ".webp"

        image_hash = hashlib.md5(
            image_url.encode(
                "utf-8"
            )
        ).hexdigest()

        filename = (
            f"/tmp/"
            f"nhl_{image_hash}"
            f"{extension}"
        )

        with open(
            filename,
            "wb"
        ) as file:

            file.write(
                content
            )

        print(
            "[IMAGE] Downloaded: "
            f"{filename}"
        )

        return {
            "file": filename,
            "url": image_url
        }

    except Exception as error:

        print(
            "[IMAGE] Download error: "
            f"{error}"
        )

        continue

print(
    "[IMAGE] All candidates failed "
    "to download."
)

return None
```

# =============================================================================

# TELEGRAM

# =============================================================================

def telegram_send_photo(
image_file,
caption
):
if not TELEGRAM_TOKEN:
raise RuntimeError(
"TOKEN is not configured"
)

```
if not TELEGRAM_CHAT_ID:
    raise RuntimeError(
        "CHAT_ID is not configured"
    )

url = (
    "https://api.telegram.org/"
    f"bot{TELEGRAM_TOKEN}/sendPhoto"
)

with open(
    image_file,
    "rb"
) as image:

    response = requests.post(
        url,
        data={
            "chat_id":
                TELEGRAM_CHAT_ID,
            "caption":
                caption
        },
        files={
            "photo":
                image
        },
        timeout=TELEGRAM_TIMEOUT
    )

if response.status_code != 200:

    raise RuntimeError(
        f"Telegram HTTP "
        f"{response.status_code}: "
        f"{response.text[:1000]}"
    )

data = response.json()

if not data.get(
    "ok",
    False
):

    raise RuntimeError(
        f"Telegram error: "
        f"{data}"
    )

return data
```

# =============================================================================

# POST CLEANING

# =============================================================================

def clean_post(
text
):
if not text:
return ""

```
text = str(
    text
).strip()

text = re.sub(
    r"^(POST|TEXT)\s*:\s*",
    "",
    text,
    flags=re.IGNORECASE
)

text = re.sub(
    r"\s+",
    " ",
    text
)

return text.strip()
```

# =============================================================================

# PROCESS ONE NEWS

# =============================================================================

def process_news(
entry
):
title = entry.get(
"title",
""
)

```
url = normalize_url(
    entry.get(
        "link",
        ""
    )
)

summary = entry.get(
    "summary",
    ""
)

print()
print(
    "=" * 70
)

print(
    "[NEWS] "
    f"{title}"
)

print(
    "[URL] "
    f"{url}"
)

print(
    "=" * 70
)

if not url:

    print(
        "[SKIP] Empty URL."
    )

    return False

if is_news_processed(
    url
):

    print(
        "[SKIP] Already processed."
    )

    return False

save_news(
    url,
    title,
    processed=False
)

article_text = get_article_text(
    entry
)

try:

    ai_result = translate_tweet(
        title,
        article_text
    )

    post = clean_post(
        ai_result.get(
            "post",
            ""
        )
    )

    search_query = str(
        ai_result.get(
            "search_query",
            ""
        )
    ).strip()

    print(
        "[POST]"
    )

    print(
        post
    )

    print(
        "[SEARCH_QUERY] "
        f"{search_query}"
    )

except Exception as error:

    print(
        "[PROCESS ERROR] "
        f"{error}"
    )

    log_error(
        url,
        error
    )

    return False

image = None

try:

    image = download_image(
        search_query
    )

except Exception as error:

    print(
        "[IMAGE ERROR] "
        f"{error}"
    )

    log_error(
        url,
        error
    )

if not image:

    print(
        "[SKIP] No suitable image."
    )

    log_error(
        url,
        "No suitable image found"
    )

    return False

try:

    telegram_send_photo(
        image["file"],
        post
    )

    print(
        "[TELEGRAM] Published."
    )

    mark_news_processed(
        url
    )

    save_image(
        image["url"],
        used=True
    )

    print(
        "[DATABASE] News marked "
        "as processed."
    )

    print(
        "[DATABASE] Image marked "
        "as used."
    )

    return True

except Exception as error:

    print(
        "[TELEGRAM ERROR] "
        f"{error}"
    )

    log_error(
        url,
        error
    )

    return False
```

# =============================================================================

# MAIN

# =============================================================================

def main():

```
print()
print(
    "=" * 70
)

print(
    "NHL NEWS BOT START"
)

print(
    "=" * 70
)

print(
    f"[CONFIG] RSS entries limit: "
    f"{MAX_RSS_ENTRIES}"
)

print(
    f"[CONFIG] Gemini primary: "
    f"{GEMINI_PRIMARY_MODEL}"
)

print(
    f"[CONFIG] Gemini fallback: "
    f"{GEMINI_FALLBACK_MODEL}"
)

print(
    "[CONFIG] Image search: "
    "context + source + date scoring"
)

print(
    "=" * 70
)

try:

    entries = get_rss_entries()

except Exception as error:

    print(
        "[RSS ERROR] "
        f"{error}"
    )

    log_error(
        "",
        error
    )

    return

if not entries:

    print(
        "[RSS] No entries."
    )

    return

new_entries = []

for entry in entries:

    url = normalize_url(
        entry.get(
            "link",
            ""
        )
    )

    if not url:
        continue

    if is_news_processed(
        url
    ):
        continue

    new_entries.append(
        entry
    )

print(
    f"[RSS] New news: "
    f"{len(new_entries)}"
)

if not new_entries:

    print(
        "[BOT] Nothing to publish."
    )

    return

new_entries.reverse()

published = 0
failed = 0

for index, entry in enumerate(
    new_entries,
    start=1
):

    print()
    print(
        f"[BOT] Processing "
        f"{index}/{len(new_entries)}"
    )

    try:

        success = process_news(
            entry
        )

        if success:
            published += 1
        else:
            failed += 1

    except Exception as error:

        failed += 1

        print(
            "[FATAL ITEM ERROR] "
            f"{error}"
        )

        log_error(
            entry.get(
                "link",
                ""
            ),
            error
        )

    time.sleep(
        GEMINI_DELAY
    )

print()
print(
    "=" * 70
)

print(
    "NHL NEWS BOT FINISHED"
)

print(
    f"Published: {published}"
)

print(
    f"Failed: {failed}"
)

print(
    "=" * 70
)
```

if **name** == "**main**":
main()
