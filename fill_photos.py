"""
Одноразовый скрипт: скачивает фото (через headless-браузер, как в
основном боте) для записей в базе, у которых их ещё нет -- то есть для
всех квартир, добавленных до появления функции сохранения фото.

Обрабатывает только активные (is_active=1) квартиры -- у пропавших
страница на sreality.cz уже не отвечает нормально, фото всё равно не
скачать.

ПЕРЕД ЗАПУСКОМ:
    pip install requests playwright
    playwright install chromium

ЗАПУСК:
    python fill_photos.py
"""

import os
import re
import json
import time
import sqlite3
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

DB_FILE = "sreality_seen.db"
PHOTOS_DIR = "photos"
MAX_PHOTOS_TO_KEEP = 3

HEADERS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def build_slug(city_part, street):
    """
    Примерный slug из района/улицы (диакритика убирается). Не обязан
    быть точным -- sreality.cz резолвит страницу по ID в конце пути
    независимо от текста slug'а, это уже проверено вручную ранее.
    """
    def to_slug(text):
        text = (text or "").lower()
        replacements = {
            "á": "a", "č": "c", "ď": "d", "é": "e", "ě": "e", "í": "i",
            "ň": "n", "ó": "o", "ř": "r", "š": "s", "ť": "t", "ú": "u",
            "ů": "u", "ý": "y", "ž": "z",
        }
        for a, b in replacements.items():
            text = text.replace(a, b)
        return re.sub(r"[^a-z0-9\-]", "", "-".join(text.split()))

    combined = f"{city_part} {street}" if street else (city_part or "hradec-kralove")
    slug = to_slug(combined)
    return slug or "hradec-kralove"


def ensure_schema(conn):
    cur = conn.execute("PRAGMA table_info(seen_listings)")
    columns = [row[1] for row in cur.fetchall()]
    if "local_photos_json" not in columns:
        print("Добавляю колонку local_photos_json в базу (миграция)...")
        conn.execute("ALTER TABLE seen_listings ADD COLUMN local_photos_json TEXT")
        conn.commit()


def fetch_photos_via_browser(hash_id, slug, max_photos=MAX_PHOTOS_TO_KEEP):
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    detail_url = f"https://www.sreality.cz/detail/pronajem/byt/1+kk/{slug}/{hash_id}"
    saved_paths = []

    def handle_response(response):
        if len(saved_paths) >= max_photos:
            return
        url = response.url
        # настоящие фото квартиры всегда с этого поддомена CDN -- всё
        # остальное (d49-a.sdn.cz и т.п.) общие элементы интерфейса сайта
        if urlparse(url).netloc != "d18-a.sdn.cz":
            return
        if response.status != 200:
            return
        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("image/"):
            return
        try:
            body = response.body()
        except Exception:
            return
        if len(body) < 5000:
            return
        idx = len(saved_paths)
        path = os.path.join(PHOTOS_DIR, f"{hash_id}_{idx}.jpg")
        with open(path, "wb") as f:
            f.write(body)
        saved_paths.append(path)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()

            for attempt in range(2):  # одна повторная попытка, если совсем ничего не скачалось
                page = browser.new_page(user_agent=HEADERS_USER_AGENT)
                page.on("response", handle_response)
                try:
                    # networkidle часто не срабатывает на sreality.cz (сайт держит
                    # фоновые соединения типа аналитики) -- ждём загрузки DOM и
                    # даём странице немного времени догрузить картинки вместо этого
                    page.goto(detail_url, wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(4000)
                except Exception as e:
                    print(f"  Предупреждение при загрузке страницы (попытка {attempt + 1}): {e}")
                page.close()

                if saved_paths:
                    break
                if attempt == 0:
                    print("  Ничего не скачалось, пробую ещё раз...")

            browser.close()
    except Exception as e:
        print(f"  Ошибка headless-браузера: {e}")

    return saved_paths


def main():
    conn = sqlite3.connect(DB_FILE)
    ensure_schema(conn)
    cur = conn.cursor()

    cur.execute("""
        SELECT hash_id, name, city_part, street FROM seen_listings
        WHERE is_active = 1
          AND (local_photos_json IS NULL OR local_photos_json = '' OR local_photos_json = '[]')
    """)
    rows = cur.fetchall()
    print(f"Найдено {len(rows)} активных записей без фото.\n")

    updated = 0
    failed = 0

    for i, (hash_id, name, city_part, street) in enumerate(rows, 1):
        print(f"[{i}/{len(rows)}] {name} ({city_part}, {street}), id={hash_id}")
        slug = build_slug(city_part, street)
        photos = fetch_photos_via_browser(hash_id, slug)

        if photos:
            photos_json = json.dumps(photos, ensure_ascii=False)
            cur.execute(
                "UPDATE seen_listings SET local_photos_json = ? WHERE hash_id = ?",
                (photos_json, hash_id),
            )
            print(f"  -> скачано {len(photos)} фото")
            updated += 1
        else:
            print("  -> не удалось скачать ни одного фото")
            failed += 1

        conn.commit()
        time.sleep(1)  # не долбим sreality.cz запросами подряд

    conn.close()
    print(f"\nГотово. Обновлено: {updated}. Не удалось: {failed}.")


if __name__ == "__main__":
    main()