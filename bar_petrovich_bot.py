"""
Бот для Telegram-канала "Барный Петрович".

Как это работает:
- Раз в день (в DRAFT_HOUR) бот готовит NUM_DRAFTS новостей про пиво, с картинками,
  и присылает их тебе лично в Телеграм (не в канал!) — каждую с кнопками
  "Опубликовать" и "Удалить".
- Ты нажимаешь нужную кнопку под каждым постом:
    - "Опубликовать" -> бот публикует именно эту новость в канал
    - "Удалить" -> черновик просто удаляется, в канал не идёт
- Можно запросить черновики вручную в любой момент, отправив боту в личку
  команду /drafts.
"""

import os
import re
import html
import json
import time
import base64
import random
import logging
import requests
import feedparser
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# ====== НАСТРОЙКИ ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН_ОТ_BOTFATHER")
CHANNEL = os.getenv("CHANNEL", "@barniy_petrovich")       # канал, куда публикуем
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID", "")             # твой личный Telegram ID

POSTED_FILE = "posted.json"
DRAFTS_FILE = "drafts.json"
OFFSET_FILE = "offset.json"

# Прямые RSS-ленты пивных сайтов — у них настоящие ссылки, картинки и описания
# "из коробки", без плясок с редиректами Google News.
DIRECT_FEEDS = [
    "https://www.hopculture.com/feed/",
    "https://beertoday.co.uk/feed/",
    "https://www.goodbeerhunting.com/blog?format=rss",
    "https://thefullpint.com/feed/",
    "https://www.canadianbeernews.com/feed/",
]

# Google News — как дополнительный источник (может быть менее надёжным по картинкам)
QUERIES = ["пиво новости", "craft beer news", "пивоварня", "крафтовое пиво", "пивной рынок"]

DRAFT_HOUR = 9     # во сколько бот сам присылает пачку черновиков
NUM_DRAFTS = 5     # сколько черновиков готовить за раз
DESCRIPTION_MAX_LEN = 700
# ========================

API = f"https://api.telegram.org/bot{BOT_TOKEN}"


# ---------- хранение данных ----------

def _load(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def _save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def load_posted():
    return set(_load(POSTED_FILE, []))


def save_posted(posted):
    _save(POSTED_FILE, list(posted)[-500:])


def load_drafts():
    return _load(DRAFTS_FILE, {})


def save_drafts(drafts):
    _save(DRAFTS_FILE, drafts)


# ---------- вспомогательное ----------

def strip_html(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()


def clean_text(text, max_len=None):
    text = html.unescape(strip_html(text))
    text = re.sub(r"\s+", " ", text).strip()
    if not max_len or len(text) <= max_len:
        return text

    cut = text[:max_len]
    # ищем последнюю точку конца предложения (. ! ?) в пределах лимита
    match = None
    for m in re.finditer(r"[.!?](?:\s|$)", cut):
        match = m
    if match:
        return cut[:match.end()].strip()

    # если законченного предложения нет вообще — обрезаем по границе слова с многоточием
    return cut.rsplit(" ", 1)[0].strip() + "…"


def clean_title(title):
    """Убирает хвост вида ' - Источник' или ' – Источник' из заголовка (для Google News)."""
    return re.split(r"\s[-–]\s(?=[^-–]*$)", title)[0].strip()


def translate_to_ru(text):
    """Переводит текст на русский через бесплатный Google Translate endpoint."""
    if not text:
        return text
    try:
        resp = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={
                "client": "gtx",
                "sl": "auto",
                "tl": "ru",
                "dt": "t",
                "q": text,
            },
            headers=HEADERS,
            timeout=8,
        )
        data = resp.json()
        return "".join(chunk[0] for chunk in data[0] if chunk[0])
    except Exception as e:
        logging.info(f"Не удалось перевести текст: {e}")
        return text


# ---------- поиск новостей: прямые RSS-ленты (основной источник) ----------

def extract_image_from_entry(entry):
    # 1) media:content / media:thumbnail (стандартные RSS-теги для картинок)
    for key in ("media_content", "media_thumbnail"):
        media = entry.get(key)
        if media:
            url = media[0].get("url")
            if url:
                return url

    # 2) enclosure с типом image/*
    for enc in entry.get("links", []):
        if enc.get("rel") == "enclosure" and "image" in enc.get("type", ""):
            return enc.get("href")

    # 3) первая картинка внутри полного текста статьи (content:encoded)
    content_list = entry.get("content")
    if content_list:
        match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', content_list[0].get("value", ""))
        if match:
            return match.group(1)

    # 4) картинка внутри summary
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', entry.get("summary", ""))
    if match:
        return match.group(1)

    return None


def fetch_direct_feed_candidates():
    posted = load_posted()
    drafted_links = {d["link"] for d in load_drafts().values()}
    candidates = []

    for feed_url in DIRECT_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
        except Exception as e:
            logging.error(f"Ошибка загрузки ленты {feed_url}: {e}")
            continue

        for entry in feed.entries:
            link = entry.get("link")
            if not link or link in posted or link in drafted_links:
                continue

            title = clean_text(entry.get("title", ""))
            description = clean_text(entry.get("summary", ""), DESCRIPTION_MAX_LEN)
            if description == title:
                description = ""

            candidates.append({
                "title": translate_to_ru(title),
                "link": link,
                "description": translate_to_ru(description),
                "image": extract_image_from_entry(entry),
            })

    random.shuffle(candidates)
    return candidates


# ---------- поиск новостей: Google News (резервный источник) ----------

def decode_google_news_url(google_link):
    """Google News кодирует настоящий адрес статьи в base64 прямо в самой ссылке
    (после /articles/). Пытаемся вытащить его напрямую, без похода на сайт."""
    try:
        path = google_link.split("/articles/")[-1].split("?")[0]
        padded = path + "=" * (-len(path) % 4)
        decoded_bytes = base64.urlsafe_b64decode(padded)
        decoded = decoded_bytes.decode("latin-1", errors="ignore")
        match = re.search(r'https?://[^\s\x00-\x1f"\\]+', decoded)
        if match:
            return match.group(0)
    except Exception:
        pass
    return None


def resolve_real_url(google_link):
    decoded = decode_google_news_url(google_link)
    if decoded and "google.com" not in decoded:
        return decoded
    try:
        resp = requests.get(google_link, headers=HEADERS, timeout=8, allow_redirects=True)
        if "google.com" not in resp.url:
            return resp.url
    except Exception:
        pass
    return None  # не удалось найти настоящую статью — лучше пропустить, чем взять мусор


def find_meta(article_url):
    """og:image и og:description настоящей статьи. Если article_url нет — не запрашиваем ничего."""
    if not article_url:
        return None, None
    image, description = None, None
    try:
        resp = requests.get(article_url, headers=HEADERS, timeout=8)
        text = resp.text
        img_match = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            text, re.IGNORECASE,
        )
        if img_match:
            image = img_match.group(1)
        desc_match = re.search(
            r'<meta[^>]+(?:property=["\']og:description["\']|name=["\']description["\'])[^>]+content=["\']([^"\']+)["\']',
            text, re.IGNORECASE,
        )
        if desc_match:
            description = clean_text(desc_match.group(1), DESCRIPTION_MAX_LEN)
    except Exception as e:
        logging.info(f"Не удалось получить данные статьи: {e}")
    return image, description


def fetch_google_news_candidates():
    posted = load_posted()
    drafted_links = {d["link"] for d in load_drafts().values()}
    seen_titles = set()
    candidates = []

    for q in QUERIES:
        url = f"https://news.google.com/rss/search?q={requests.utils.quote(q)}&hl=ru&gl=RU&ceid=RU:ru"
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            logging.error(f"Ошибка загрузки ленты по запросу '{q}': {e}")
            continue
        for entry in feed.entries:
            link = entry.link
            title = clean_title(entry.title)
            if link in posted or link in drafted_links or title in seen_titles:
                continue
            seen_titles.add(title)
            candidates.append({"title": title, "link": link})

    random.shuffle(candidates)
    return candidates


def enrich_google_news_item(item, max_attempts_left):
    real_url = resolve_real_url(item["link"])
    if not real_url:
        return None
    image, description = find_meta(real_url)
    item["image"] = image
    item["description"] = translate_to_ru(description) if description else description
    return item


# ---------- сборка черновиков ----------

def build_drafts(n=NUM_DRAFTS, max_google_attempts=10):
    """Сначала берём проверенные прямые RSS-ленты, при нехватке — добираем из Google News."""
    results = fetch_direct_feed_candidates()[:n]

    if len(results) < n:
        gn_candidates = fetch_google_news_candidates()
        needed = n - len(results)
        checked = 0
        for item in gn_candidates:
            if needed <= 0 or checked >= max_google_attempts:
                break
            checked += 1
            enriched = enrich_google_news_item(item, max_google_attempts - checked)
            if enriched:
                results.append(enriched)
                needed -= 1

    return results


# ---------- отправка сообщений ----------

def tg_call(method, **params):
    try:
        resp = requests.post(f"{API}/{method}", data=params, timeout=15)
        return resp.json()
    except Exception as e:
        logging.error(f"Ошибка запроса {method}: {e}")
        return {}


def build_post_text(item):
    text = f"🍺 <b>{item['title']}</b>"
    description = item.get("description")
    if description:
        text += f"\n\n{description}"
    return text


def publish_to_channel(item):
    text = build_post_text(item)
    if item.get("image"):
        result = tg_call("sendPhoto", chat_id=CHANNEL, photo=item["image"],
                          caption=text, parse_mode="HTML")
        if result.get("ok"):
            return True
        logging.warning(f"Не вышло с картинкой, пробую текстом: {result}")

    result = tg_call("sendMessage", chat_id=CHANNEL, text=text, parse_mode="HTML")
    return bool(result.get("ok"))


def send_draft_to_owner(draft_id, item):
    if not OWNER_CHAT_ID:
        logging.error("OWNER_CHAT_ID не задан — некому присылать черновики")
        return
    text = build_post_text(item)
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Опубликовать", "callback_data": f"pub:{draft_id}"},
            {"text": "🗑 Удалить", "callback_data": f"del:{draft_id}"},
        ]]
    }
    if item.get("image"):
        tg_call("sendPhoto", chat_id=OWNER_CHAT_ID, photo=item["image"],
                caption=text, parse_mode="HTML", reply_markup=json.dumps(keyboard))
    else:
        tg_call("sendMessage", chat_id=OWNER_CHAT_ID, text=text,
                parse_mode="HTML", reply_markup=json.dumps(keyboard))


def send_drafts(n=NUM_DRAFTS):
    items = build_drafts(n)
    if not items:
        logging.warning("Не нашлось новых новостей для черновиков")
        if OWNER_CHAT_ID:
            tg_call("sendMessage", chat_id=OWNER_CHAT_ID,
                    text="Не нашлось новых новостей для черновиков 🤷")
        return

    drafts = load_drafts()
    for item in items:
        draft_id = str(int(time.time() * 1000)) + str(random.randint(10, 99))
        drafts[draft_id] = item
        send_draft_to_owner(draft_id, item)
        time.sleep(1)
    save_drafts(drafts)
    logging.info(f"Отправлено {len(items)} черновиков владельцу")


# ---------- обработка кнопок и команд ----------

def handle_callback(callback):
    data = callback.get("data", "")
    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    callback_id = callback.get("id")

    tg_call("answerCallbackQuery", callback_query_id=callback_id)

    if ":" not in data:
        return
    action, draft_id = data.split(":", 1)
    drafts = load_drafts()
    item = drafts.get(draft_id)
    if not item:
        return

    if action == "pub":
        ok = publish_to_channel(item)
        result_text = "✅ Опубликовано в канале" if ok else "⚠️ Ошибка публикации, попробуй ещё раз"
        if ok:
            posted = load_posted()
            posted.add(item["link"])
            save_posted(posted)
    else:
        result_text = "🗑 Черновик удалён"

    del drafts[draft_id]
    save_drafts(drafts)

    caption = f"{build_post_text(item)}\n\n{result_text}"
    if item.get("image"):
        tg_call("editMessageCaption", chat_id=chat_id, message_id=message_id,
                caption=caption, parse_mode="HTML")
    else:
        tg_call("editMessageText", chat_id=chat_id, message_id=message_id,
                text=caption, parse_mode="HTML")


def handle_message(message):
    text = (message.get("text") or "").strip()
    chat_id = message.get("chat", {}).get("id")

    if text == "/start":
        tg_call("sendMessage", chat_id=chat_id,
                text=f"Привет! Твой chat_id: {chat_id}\n"
                     f"Пропиши его в переменную OWNER_CHAT_ID, чтобы получать черновики постов.")
    elif text == "/drafts":
        if OWNER_CHAT_ID and str(chat_id) != str(OWNER_CHAT_ID):
            tg_call("sendMessage", chat_id=chat_id, text="Эта команда доступна только владельцу бота.")
            return
        tg_call("sendMessage", chat_id=chat_id, text="Готовлю черновики, минутку…")
        send_drafts()


def poll_updates():
    offset = _load(OFFSET_FILE, {}).get("offset", 0)
    result = tg_call("getUpdates", offset=offset, timeout=20)
    for update in result.get("result", []):
        offset = update["update_id"] + 1
        if "callback_query" in update:
            handle_callback(update["callback_query"])
        elif "message" in update:
            handle_message(update["message"])
    _save(OFFSET_FILE, {"offset": offset})


# ---------- главный цикл ----------

def main():
    logging.info("Бот 'Барный Петрович' запущен")
    last_draft_date = None

    while True:
        now = datetime.now()

        if now.hour == DRAFT_HOUR and last_draft_date != now.date():
            send_drafts()
            last_draft_date = now.date()

        poll_updates()  # тут же ловим долгий poll (до 20 сек), поэтому отдельный sleep не нужен


if __name__ == "__main__":
    main()

