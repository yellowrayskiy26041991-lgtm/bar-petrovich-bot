"""
Бот для Telegram-канала "Барный Петрович".
Ищет свежие новости про пиво/крафтовое пивоварение и публикует их
в канал 3 раза в день в случайное время (с 8:00 до 23:00, ночью не постит).

Как это работает:
- Каждый день в 00:00 бот выбирает 3 случайных времени публикации.
- В нужный момент берёт свежую (ещё не опубликованную) новость и постит в канал.
- Уже опубликованные ссылки запоминаются в файле posted.json, чтобы не дублировать.
"""

import os
import re
import json
import time
import random
import logging
import requests
import feedparser
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# ====== НАСТРОЙКИ ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН_ОТ_BOTFATHER")
CHANNEL = os.getenv("CHANNEL", "@barniy_petrovich")  # username канала или числовой ID (-100...)

POSTED_FILE = "posted.json"
QUERIES = ["пиво новости", "craft beer news", "пивоварня", "крафтовое пиво", "пивной рынок"]

DAY_START_HOUR = 8   # раньше не постим
DAY_END_HOUR = 23    # позже не постим (ночью — тишина)
POSTS_PER_DAY = 3
# ========================


def load_posted():
    if os.path.exists(POSTED_FILE):
        with open(POSTED_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_posted(posted):
    with open(POSTED_FILE, "w", encoding="utf-8") as f:
        json.dump(list(posted)[-500:], f, ensure_ascii=False)


def clean_title(title):
    """Убирает хвост вида ' - Источник' или ' – Источник' из заголовка Google News."""
    return re.split(r"\s[-–]\s(?=[^-–]*$)", title)[0].strip()


def resolve_real_url(google_link):
    """Разворачивает редирект Google News до настоящего адреса статьи."""
    try:
        resp = requests.get(google_link, headers=HEADERS, timeout=8, allow_redirects=True)
        return resp.url
    except Exception:
        return google_link


def find_image(article_url):
    """Пытается найти og:image на странице статьи."""
    try:
        resp = requests.get(article_url, headers=HEADERS, timeout=8)
        match = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            resp.text,
            re.IGNORECASE,
        )
        if match:
            return match.group(1)
    except Exception as e:
        logging.info(f"Не удалось получить картинку: {e}")
    return None


def fetch_news():
    posted = load_posted()
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
            if link not in posted:
                candidates.append({
                    "title": clean_title(entry.title),
                    "link": link,
                })
    random.shuffle(candidates)
    return candidates, posted


def send_to_channel(item):
    text = f"🍺 <b>{item['title']}</b>"

    real_url = resolve_real_url(item["link"])
    image_url = find_image(real_url)

    if image_url:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        resp = requests.post(url, data={
            "chat_id": CHANNEL,
            "photo": image_url,
            "caption": text,
            "parse_mode": "HTML",
        })
        if resp.ok:
            logging.info(f"Опубликовано с картинкой: {item['title']}")
            return True
        logging.warning(f"Не получилось отправить с картинкой, пробую без: {resp.text}")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": CHANNEL,
        "text": text,
        "parse_mode": "HTML",
    })
    if resp.ok:
        logging.info(f"Опубликовано: {item['title']}")
        return True
    logging.error(f"Ошибка отправки: {resp.text}")
    return False


def post_one():
    candidates, posted = fetch_news()
    if not candidates:
        logging.warning("Нет свежих новостей для публикации")
        return
    item = candidates[0]
    if send_to_channel(item):
        posted.add(item["link"])
        save_posted(posted)


def plan_today_times():
    minutes_range = range(DAY_START_HOUR * 60, DAY_END_HOUR * 60)
    chosen = sorted(random.sample(minutes_range, POSTS_PER_DAY))
    return [(m // 60, m % 60) for m in chosen]


def main():
    logging.info("Бот 'Барный Петрович' запущен")
    current_day = None
    today_times = []
    done_today = set()

    while True:
        now = datetime.now()
        if current_day != now.date():
            current_day = now.date()
            today_times = plan_today_times()
            done_today = set()
            readable = [f"{h:02d}:{m:02d}" for h, m in today_times]
            logging.info(f"План публикаций на {current_day}: {readable}")

        for t in today_times:
            if t not in done_today and (now.hour, now.minute) == t:
                post_one()
                done_today.add(t)

        time.sleep(30)


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
