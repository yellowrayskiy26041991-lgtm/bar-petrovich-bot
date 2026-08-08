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

QUERIES = ["пиво новости", "craft beer news", "пивоварня", "крафтовое пиво", "пивной рынок"]

DRAFT_HOUR = 9     # во сколько бот сам присылает пачку черновиков
NUM_DRAFTS = 5     # сколько черновиков готовить за раз
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


# ---------- поиск новостей ----------

def clean_title(title):
    """Убирает хвост вида ' - Источник' или ' – Источник' из заголовка Google News."""
    return re.split(r"\s[-–]\s(?=[^-–]*$)", title)[0].strip()


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
    """Разворачивает ссылку Google News до настоящего адреса статьи."""
    decoded = decode_google_news_url(google_link)
    if decoded and "google.com" not in decoded:
        return decoded

    try:
        resp = requests.get(google_link, headers=HEADERS, timeout=8, allow_redirects=True)
        html = resp.text

        match = re.search(
            r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\'][^"\']*url=([^"\']+)["\']',
            html, re.IGNORECASE,
        )
        if match and "google.com" not in match.group(1):
            return match.group(1)

        match = re.search(r'(?:window\.location\.href|window\.location\.replace)\(?=?\s*["\']([^"\']+)["\']', html)
        if match and "google.com" not in match.group(1):
            return match.group(1)

        if "google.com" not in resp.url:
            return resp.url
    except Exception:
        pass

    return None  # не удалось найти настоящую статью


def find_meta(article_url):
    """Пытается найти картинку (og:image) и краткое описание (og:description) статьи.
    Если реальный адрес не найден (article_url is None) — ничего не запрашиваем,
    чтобы не утащить данные самого Google News."""
    if not article_url:
        return None, None

    image, description = None, None
    try:
        resp = requests.get(article_url, headers=HEADERS, timeout=8)
        html = resp.text

        img_match = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            html, re.IGNORECASE,
        )
        if img_match:
            image = img_match.group(1)

        desc_match = re.search(
            r'<meta[^>]+(?:property=["\']og:description["\']|name=["\']description["\'])[^>]+content=["\']([^"\']+)["\']',
            html, re.IGNORECASE,
        )
        if desc_match:
            description = desc_match.group(1).strip()
    except Exception as e:
        logging.info(f"Не удалось получить данные статьи: {e}")
    return image, description




def strip_html(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()


def search_candidates():
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
            rss_summary = strip_html(getattr(entry, "summary", ""))
            candidates.append({"title": title, "link": link, "rss_summary": rss_summary})

    random.shuffle(candidates)
    return candidates


def build_drafts(n=NUM_DRAFTS, max_attempts=15):
    """Ищет новости и собирает n штук с картинками и описанием (если получится)."""
    candidates = search_candidates()
    results = []
    for item in candidates[:max_attempts]:
        real_url = resolve_real_url(item["link"])
        image, description = find_meta(real_url)
        item["image"] = image
        item["description"] = description or item.get("rss_summary") or ""
        results.append(item)
        if len(results) >= n:
            break
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

