"""
Основной скрипт бота: проверяет sreality.cz на новые И пропавшие
объявления 1+kk (pronajem) в Hradec Kralove, шлёт уведомления в Telegram
с фото (для новых) и полным описанием + фото (для пропавших).

Фото скачиваются через headless-браузер (Playwright), а не обычными
HTTP-запросами -- CDN фото sreality.cz (sdn.cz) блокирует прямые запросы
(401 Unauthorized) даже с правильными заголовками/Referer, похоже на
защиту по TLS-отпечатку, которую можно обойти только настоящим браузером.

Логика:
    1. Запрашивает ВСЕ страницы поиска (полный текущий список активных
       объявлений)
    2. Сравнивает с тем, что в базе помечено как активное (is_active=1)
    3. Новые ID -> открывает страницу квартиры в headless-браузере,
       перехватывает фото из сетевых ответов, шлёт альбомом + ссылка,
       is_active=1
    4. Пропавшие ID -> "Квартира больше недоступна" с сохранённым фото
       и полным описанием (ссылка на само объявление уже не откроется)

ПЕРЕД ЗАПУСКОМ:
    pip install requests playwright
    playwright install chromium

Убедись, что sreality_seen.db создана свежим init_db.py.

ЗАПУСК ОДНОГО ПРОХОДА:
    python check_new_listings.py
"""

import re
import math
import os
import json
import requests
import sqlite3
import time
import sys
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from playwright.sync_api import sync_playwright

try:
    from dotenv import load_dotenv
    load_dotenv()  # подхватывает переменные из .env при локальном запуске.
                    # В GitHub Actions .env не нужен -- там секреты уже
                    # приходят через os.environ напрямую из workflow.
except ImportError:
    pass  # если python-dotenv не установлен -- просто продолжаем без .env,
          # переменные окружения могут быть заданы и другим способом

# Координаты Hradec Králové hlavní nádraží (взяты из данных sreality.cz)
STATION_LAT = 50.21475467876146
STATION_LON = 15.809999179359096


def haversine_km(lat1, lon1, lat2, lon2):
    """Расстояние по прямой между двумя точками на Земле, в км."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))

# ==== Токен и chat_id читаются из переменных окружения (см. GitHub Secrets) ====
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
# ==============================================

BUILD_ID = "1.0.513"  # запасное значение на случай, если авто-определение не сработает

DB_FILE = "sreality_seen.db"
PHOTOS_DIR = "photos"
MAX_PHOTOS_PER_LISTING = 8  # сколько скачивать и слать при обнаружении новой квартиры
MAX_PHOTOS_TO_KEEP = 3      # сколько из них оставлять на диске / в репозитории для будущего уведомления о пропаже

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "*/*",
    "x-nextjs-data": "1",
    "Referer": (
        "https://www.sreality.cz/hledani/pronajem/byty"
        "?velikost=1%2Bkk&region=Hradec%20Kr%C3%A1lov%C3%A9"
        "&region-id=2149&region-typ=municipality"
    ),
}


def fetch_current_build_id():
    """
    Автоматически определяет актуальный buildId сайта -- парсит его из
    обычной HTML-страницы поиска (Next.js всегда встраивает его туда в
    виде "buildId":"..."). Это избавляет от необходимости вручную
    обновлять BUILD_ID каждый раз, когда sreality.cz выкатывает новую
    версию сайта. Возвращает строку с buildId или None, если не удалось.
    """
    url = (
        "https://www.sreality.cz/hledani/pronajem/byty"
        "?velikost=1%2Bkk&region=Hradec%20Kr%C3%A1lov%C3%A9"
        "&region-id=2149&region-typ=municipality"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            print(f"Не удалось получить HTML страницы для определения buildId: статус {resp.status_code}")
            return None
        match = re.search(r'"buildId":"([^"]+)"', resp.text)
        if match:
            return match.group(1)
        print("Не нашёл buildId в HTML страницы.")
        return None
    except requests.RequestException as e:
        print(f"Ошибка сети при определении buildId: {e}")
        return None


def build_search_url():
    return (
        f"https://www.sreality.cz/_next/data/{BUILD_ID}/cs/hledani/pronajem/byty.json"
        "?velikost=1%2Bkk"
        "&region=Hradec+Kr%C3%A1lov%C3%A9"
        "&region-id=2149"
        "&region-typ=municipality"
        "&lat-max=50.35292204433406"
        "&lat-min=50.069053151744406"
        "&lon-max=16.005719999054993"
        "&lon-min=15.656217435578432"
        "&slug=pronajem&slug=byty"
    )


def set_query_param(url, key, value):
    parts = urlparse(url)
    q = parse_qs(parts.query)
    q[key] = [str(value)]
    new_query = urlencode(q, doseq=True)
    return urlunparse(parts._replace(query=new_query))


def find_results_and_pagination(data):
    queries = (
        data.get("pageProps", {})
            .get("dehydratedState", {})
            .get("queries", [])
    )
    for q in queries:
        state_data = q.get("state", {}).get("data")
        if isinstance(state_data, dict) and "results" in state_data:
            return state_data["results"], state_data.get("pagination", {})
    return None, None


def build_link(item):
    hash_id = item.get("id")
    return f"https://www.sreality.cz/detail/pronajem/byt/1+kk/hradec-kralove/{hash_id}"


def extract_area(name):
    if not name:
        return None
    match = re.search(r"(\d+)\s*m", name)
    if match:
        return int(match.group(1))
    return None


def fetch_page(page_number):
    base_url = build_search_url()
    url = base_url if page_number == 1 else set_query_param(base_url, "strana", page_number)
    resp = requests.get(url, headers=HEADERS, timeout=15)
    if resp.status_code != 200:
        print(f"Ошибка на странице {page_number}: статус {resp.status_code}")
        print(resp.text[:500])
        sys.exit(1)
    data = resp.json()
    return find_results_and_pagination(data)


def fetch_all_current_listings():
    all_results = []
    page = 1
    total_expected = None

    while True:
        results, pagination = fetch_page(page)
        if results is None:
            print("Не нашёл results в ответе -- структура сайта могла измениться.")
            sys.exit(1)

        if total_expected is None:
            total_expected = pagination.get("total", len(results))

        if not results:
            break

        all_results.extend(results)

        if len(all_results) >= total_expected:
            break

        page += 1
        time.sleep(1)

        if page > 20:
            break

    return all_results


def fetch_estate_detail_json(hash_id, slug="hradec-kralove", _redirect_followed=False):
    """
    Запрашивает detail-эндпоинт конкретной квартиры. Slug в URL не важен
    для отображения страницы человеку, но для JSON-эндпоинта сайт в этом
    случае возвращает не данные, а редирект (__N_REDIRECT) на правильный
    slug. Поэтому: пробуем с плейсхолдером, если получаем редирект --
    повторяем запрос уже с правильным slug (один раз, чтобы не зациклиться).
    """
    url = f"https://www.sreality.cz/_next/data/{BUILD_ID}/cs/detail/pronajem/byt/1+kk/{slug}/{hash_id}.json"
    referer = f"https://www.sreality.cz/detail/pronajem/byt/1+kk/{slug}/{hash_id}"
    headers = dict(HEADERS)
    headers["Referer"] = referer

    try:
        resp = requests.get(url, headers=headers, timeout=15)
    except requests.RequestException as e:
        print(f"Ошибка сети при запросе деталей {hash_id}: {e}")
        return None

    if resp.status_code != 200:
        return None

    data = resp.json()
    redirect = data.get("pageProps", {}).get("__N_REDIRECT")

    if redirect and not _redirect_followed:
        parts = redirect.rstrip("/").split("/")
        if len(parts) >= 2:
            real_id = parts[-1]
            real_slug = parts[-2]
            return fetch_estate_detail_json(real_id, real_slug, _redirect_followed=True)

    return data


def extract_description(detail_data):
    if not detail_data:
        return None
    queries = (
        detail_data.get("pageProps", {})
                   .get("dehydratedState", {})
                   .get("queries", [])
    )
    for q in queries:
        key = q.get("queryKey", [])
        if key and key[0] == "estate":
            state_data = q.get("state", {}).get("data")
            if isinstance(state_data, dict):
                return state_data.get("description")
    return None


def fetch_description(hash_id):
    """Возвращает текст описания квартиры или None, если не удалось получить."""
    detail_data = fetch_estate_detail_json(hash_id)
    return extract_description(detail_data)


def fetch_photos_via_browser(hash_id, slug="hradec-kralove", max_photos=MAX_PHOTOS_PER_LISTING):
    """
    Открывает страницу квартиры в headless Chromium (Playwright) и
    перехватывает фото прямо из сетевых ответов браузера -- это байты,
    которые реально получил бы человек, открывший страницу, поэтому
    никакая защита CDN на них не сработает (в отличие от прямых
    HTTP-запросов через requests, которые CDN блокирует).

    Возвращает список путей к сохранённым локальным файлам.
    """
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    detail_url = f"https://www.sreality.cz/detail/pronajem/byt/1+kk/{slug}/{hash_id}"
    saved_paths = []

    def handle_response(response):
        if len(saved_paths) >= max_photos:
            return
        url = response.url
        # Настоящие фото квартиры всегда приходят с этого поддомена CDN.
        # Остальные (d49-a.sdn.cz, d24-a.sdn.cz и т.п.) -- общие элементы
        # интерфейса сайта (баннеры, иконки), одни и те же на разных
        # объявлениях, а не фото конкретной квартиры.
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
        if len(body) < 5000:  # на всякий случай отсекаем мелкие иконки
            return
        idx = len(saved_paths)
        path = f"{PHOTOS_DIR}/{hash_id}_{idx}.jpg"
        with open(path, "wb") as f:
            f.write(body)
        saved_paths.append(path)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()

            for attempt in range(2):  # одна повторная попытка, если совсем ничего не скачалось
                page = browser.new_page(user_agent=HEADERS["User-Agent"])
                page.on("response", handle_response)
                try:
                    # networkidle часто не срабатывает на sreality.cz (сайт держит
                    # фоновые соединения типа аналитики) -- ждём загрузки DOM и
                    # даём странице немного времени догрузить картинки вместо этого
                    page.goto(detail_url, wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(4000)
                except Exception as e:
                    print(f"Предупреждение при загрузке {detail_url} (попытка {attempt + 1}): {e}")
                page.close()

                if saved_paths:
                    break
                if attempt == 0:
                    print("  Ничего не скачалось, пробую ещё раз...")

            browser.close()
    except Exception as e:
        print(f"Ошибка headless-браузера при получении фото для {hash_id}: {e}")

    return saved_paths


def ensure_schema(conn):
    cur = conn.execute("PRAGMA table_info(seen_listings)")
    columns = [row[1] for row in cur.fetchall()]

    migrations = {
        "is_active": "INTEGER DEFAULT 1",
        "description": "TEXT",
        "area_m2": "INTEGER",
        "street": "TEXT",
        "distance_to_station_km": "REAL",
        "local_photos_json": "TEXT",
    }

    for col, col_type in migrations.items():
        if col not in columns:
            print(f"Добавляю колонку {col} в базу (миграция)...")
            conn.execute(f"ALTER TABLE seen_listings ADD COLUMN {col} {col_type}")
    conn.commit()


TELEGRAM_CAPTION_LIMIT = 1024  # лимит Telegram для подписи к фото/альбому (у обычных сообщений 4096)


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=10,
    )
    data = resp.json()
    if not data.get("ok"):
        print("Не удалось отправить сообщение в Telegram:")
        print(data)


def split_caption_for_media(caption, limit=TELEGRAM_CAPTION_LIMIT):
    """
    Если подпись длиннее лимита Telegram для фото/альбома -- обрезает её
    для использования как caption у фото, а ОСТАТОК (не всё целиком)
    возвращает отдельно, чтобы отправить его следующим сообщением --
    без повтора уже показанной части.
    Возвращает (short_caption, remainder_or_None).
    """
    if len(caption) <= limit:
        return caption, None
    marker = "\n\n(продолжение ниже)"
    cut = limit - len(marker)
    short_caption = caption[:cut].rstrip() + marker
    remainder = caption[cut:].lstrip()
    return short_caption, remainder


def send_telegram_local_photo(local_path, caption):
    """Отправляет одно фото, загружая его как файл с диска."""
    if not local_path or not os.path.exists(local_path):
        return False
    api_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        with open(local_path, "rb") as f:
            resp = requests.post(
                api_url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"photo": f},
                timeout=20,
            )
        data = resp.json()
        if data.get("ok"):
            return True
        print(f"Не удалось отправить фото {local_path}:")
        print(data)
        return False
    except (requests.RequestException, OSError) as e:
        print(f"Ошибка при отправке фото {local_path}: {e}")
        return False


def send_telegram_photos_album(local_paths, caption):
    """
    Отправляет альбом, загружая файлы с диска. Если фото всего одно,
    шлёт как обычное фото (sendMediaGroup требует минимум 2 элемента).
    Если подпись длиннее лимита Telegram (1024 символа) -- обрезает её
    для фото и шлёт полный текст отдельным сообщением следом.
    Возвращает True, если фото отправились успешно.
    """
    short_caption, remainder_text = split_caption_for_media(caption)

    if not local_paths:
        send_telegram_message(caption)  # тут лимит уже 4096, обрезка не нужна
        return False

    if len(local_paths) == 1:
        success = send_telegram_local_photo(local_paths[0], short_caption)
        if not success:
            send_telegram_message(caption)
            return False
        if remainder_text:
            send_telegram_message(remainder_text)
        return True

    media = []
    files = {}
    opened_files = []
    try:
        for i, path in enumerate(local_paths):
            key = f"photo{i}"
            f = open(path, "rb")
            opened_files.append(f)
            files[key] = f
            item = {"type": "photo", "media": f"attach://{key}"}
            if i == 0:
                item["caption"] = short_caption
            media.append(item)

        api_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMediaGroup"
        resp = requests.post(
            api_url,
            data={"chat_id": TELEGRAM_CHAT_ID, "media": json.dumps(media)},
            files=files,
            timeout=30,
        )
        data = resp.json()
        if data.get("ok"):
            if remainder_text:
                send_telegram_message(remainder_text)
            return True
        print("Не удалось отправить альбом:")
        print(data)
        send_telegram_message(caption)
        return False
    except (requests.RequestException, OSError) as e:
        print(f"Ошибка при отправке альбома: {e}")
        send_telegram_message(caption)
        return False
    finally:
        for f in opened_files:
            f.close()


def main():
    global BUILD_ID

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_TOKEN или TELEGRAM_CHAT_ID не заданы!")
        print("Локально: проверь файл .env (и что установлен python-dotenv).")
        print("В GitHub Actions: проверь Settings -> Secrets and variables -> Actions.")
        sys.exit(1)

    detected_build_id = fetch_current_build_id()
    if detected_build_id:
        BUILD_ID = detected_build_id
        print(f"Определил текущий buildId сайта: {BUILD_ID}")
    else:
        print(f"Не удалось определить buildId автоматически, использую резервный: {BUILD_ID}")

    conn = sqlite3.connect(DB_FILE)
    ensure_schema(conn)
    cur = conn.cursor()

    print("Запрашиваю sreality.cz (все страницы)...")
    current_listings = fetch_all_current_listings()
    print(f"Сейчас активно на сайте: {len(current_listings)} объявлений.")

    now = int(time.time())
    current_ids = set()
    new_count = 0

    # 1) проходим по текущим объявлениям, ищем новые
    for item in current_listings:
        hash_id = str(item.get("id"))
        current_ids.add(hash_id)

        name = item.get("name", "")
        locality = item.get("locality", {}) or {}
        city_part = locality.get("cityPart", "")
        street = locality.get("street", "") or ""
        price = item.get("priceCzk")
        area = extract_area(name)
        link = build_link(item)

        lat = locality.get("latitude")
        lon = locality.get("longitude")
        distance_km = round(haversine_km(lat, lon, STATION_LAT, STATION_LON), 2) if lat and lon else None
        distance_text = f"{distance_km} км до вокзала (по прямой)" if distance_km is not None else "расстояние до вокзала неизвестно"

        # настоящий slug нужен для Referer'а, который видит headless-браузер
        city_part_seo = locality.get("cityPartSeoName", "hradec-kralove")
        district_seo = locality.get("districtSeoName", "hradec-kralove")
        real_slug = f"{city_part_seo}-{district_seo}"

        cur.execute(
            "SELECT is_active FROM seen_listings WHERE hash_id = ?", (hash_id,)
        )
        row = cur.fetchone()

        if row is None:
            new_count += 1
            print(f"НОВОЕ: {name} -- {link}")

            caption = (
                f"Новая квартира 1+kk в Hradec Kralove!\n"
                f"{name}\n{city_part}, {street}\n{price} Kc/mesic\n"
                f"{distance_text}\n{link}"
            )
            send_telegram_message(caption)

            # фото и описание молча скачиваем и сохраняем про запас --
            # понадобятся позже, если квартиру снимут с публикации.
            # В уведомление о НОВОЙ квартире их не включаем.
            print("  скачиваю фото через браузер (для будущего уведомления о пропаже)...")
            local_photos = fetch_photos_via_browser(hash_id, real_slug, max_photos=MAX_PHOTOS_TO_KEEP)
            print(f"  скачано фото: {len(local_photos)}")

            description = fetch_description(hash_id)
            local_photos_json = json.dumps(local_photos, ensure_ascii=False)

            cur.execute(
                """INSERT INTO seen_listings
                   (hash_id, name, city_part, street, price_czk, area_m2,
                    distance_to_station_km, link, description, first_seen_at,
                    is_active, local_photos_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (hash_id, name, city_part, street, price, area,
                 distance_km, link, description, now, local_photos_json),
            )
            time.sleep(1)  # не долбим API sreality.cz запросами подряд

        elif row[0] == 0:
            new_count += 1
            print(f"СНОВА ПОЯВИЛОСЬ: {name} -- {link}")

            caption = (
                f"Новая квартира 1+kk в Hradec Kralove!\n"
                f"{name}\n{city_part}, {street}\n{price} Kc/mesic\n"
                f"{distance_text}\n{link}"
            )
            send_telegram_message(caption)

            print("  скачиваю фото через браузер (для будущего уведомления о пропаже)...")
            local_photos = fetch_photos_via_browser(hash_id, real_slug, max_photos=MAX_PHOTOS_TO_KEEP)
            print(f"  скачано фото: {len(local_photos)}")

            description = fetch_description(hash_id)
            local_photos_json = json.dumps(local_photos, ensure_ascii=False)

            cur.execute(
                """UPDATE seen_listings
                   SET is_active = 1, area_m2 = ?, street = ?,
                       price_czk = ?, link = ?, description = ?,
                       distance_to_station_km = ?, local_photos_json = ?
                   WHERE hash_id = ?""",
                (area, street, price, link, description, distance_km,
                 local_photos_json, hash_id),
            )
            time.sleep(1)
        # если is_active == 1 -- уже знаем, ничего не делаем

    conn.commit()

    # 2) ищем пропавшие
    cur.execute("""
        SELECT hash_id, name, city_part, street, price_czk, link,
               area_m2, description, distance_to_station_km, local_photos_json
        FROM seen_listings WHERE is_active = 1
    """)
    removed_count = 0

    for hash_id, name, city_part, street, price, link, area, description, distance_km, local_photos_json in cur.fetchall():
        if hash_id not in current_ids:
            removed_count += 1
            distance_text = f"{distance_km} км до вокзала (по прямой)" if distance_km is not None else "расстояние до вокзала неизвестно"
            description_text = description if description else "(описание не сохранено)"
            local_photos = json.loads(local_photos_json) if local_photos_json else []
            local_photos = [p for p in local_photos if os.path.exists(p)]

            caption = (
                f"Квартира больше недоступна (сняли с публикации):\n"
                f"{name}\n"
                f"{city_part}, {street}\n"
                f"Была за {price} Kc/mesic\n"
                f"{distance_text}\n\n"
                f"{description_text}\n\n"
                f"(ссылка уже не работает: {link})"
            )
            print(f"ПРОПАЛО: {name} -- {link} (сохранённых фото: {len(local_photos)})")
            send_telegram_photos_album(local_photos, caption)

            cur.execute(
                "UPDATE seen_listings SET is_active = 0 WHERE hash_id = ?",
                (hash_id,),
            )

    conn.commit()
    conn.close()

    print(f"\nГотово. Новых: {new_count}. Пропавших: {removed_count}.")


if __name__ == "__main__":
    main()

"""
========================================
ЗАПУСК КАЖДЫЙ ЧАС НА WINDOWS (Планировщик заданий):
========================================
1. Открой "Планировщик заданий" (Task Scheduler) -- через поиск в меню Пуск
2. Создать задачу (Create Task)
3. Вкладка "Общие" (General): имя, например "Sreality Bot"
4. Вкладка "Триггеры" (Triggers) -> Создать:
   - Повторять задачу каждые: 1 час
   - Повторять в течение: неограниченного срока (Indefinitely)
5. Вкладка "Действия" (Actions) -> Создать:
   - Программа/скрипт: путь к python.exe
   - Аргументы: полный путь к этому файлу
   - Рабочая папка (Start in): папка со скриптом и базой
6. Сохранить задачу
"""