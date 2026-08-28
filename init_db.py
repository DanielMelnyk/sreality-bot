"""
Одноразовый скрипт: создаёт SQLite-базу с ID ВСЕХ текущих объявлений
1+kk (pronajem) в Hradec Kralove на sreality.cz.

Фото НЕ сохраняются -- фото-CDN sreality.cz (sdn.cz) блокирует прямой
доступ к картинкам (401 Unauthorized) даже из браузера, так что смысла
их хранить/пересылать нет. Вместо этого сохраняем описание квартиры
(текст с sreality.cz), площадь, улицу, цену -- этого достаточно, чтобы
понять, что за квартира, даже если её потом снимут с публикации.

ПЕРЕД ЗАПУСКОМ:
    pip install requests
ЗАПУСК:
    python init_db.py
"""

import re
import math
import requests
import sqlite3
import json
import time
import sys
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

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

BUILD_ID = "1.0.512"  # номер билда сайта, может со временем устареть

BASE_URL = (
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

DB_FILE = "sreality_seen.db"
RAW_DUMP_PREFIX = "sreality_raw_response_page"

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


def set_query_param(url, key, value):
    parts = urlparse(url)
    q = parse_qs(parts.query)
    q[key] = [str(value)]
    new_query = urlencode(q, doseq=True)
    return urlunparse(parts._replace(query=new_query))


def init_db(conn):
    conn.execute("DROP TABLE IF EXISTS seen_listings")
    conn.execute("""
        CREATE TABLE seen_listings (
            hash_id TEXT PRIMARY KEY,
            name TEXT,
            city_part TEXT,
            street TEXT,
            price_czk INTEGER,
            area_m2 INTEGER,
            distance_to_station_km REAL,
            link TEXT,
            description TEXT,
            first_seen_at INTEGER,
            is_active INTEGER DEFAULT 1
        )
    """)
    conn.commit()


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
    """
    Текст slug'а в URL (район/улица) sreality.cz не проверяет -- важен
    только сам ID в конце пути. Используем фиксированный плейсхолдер --
    так ссылка гарантированно рабочая для любой локации.
    """
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
    url = BASE_URL if page_number == 1 else set_query_param(BASE_URL, "strana", page_number)
    resp = requests.get(url, headers=HEADERS, timeout=15)
    if resp.status_code != 200:
        print(f"Ошибка на странице {page_number}: статус {resp.status_code}")
        print(resp.text[:500])
        sys.exit(1)
    data = resp.json()

    with open(f"{RAW_DUMP_PREFIX}{page_number}.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return find_results_and_pagination(data)


def fetch_estate_detail_json(hash_id, slug="hradec-kralove", _redirect_followed=False):
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


def main():
    conn = sqlite3.connect(DB_FILE)
    init_db(conn)

    now = int(time.time())
    total_inserted = 0
    total_expected = None
    page = 1
    all_items = []

    while True:
        print(f"Запрашиваю страницу {page}...")
        results, pagination = fetch_page(page)

        if results is None:
            print("Не нашёл results в ответе -- структура сайта могла измениться.")
            sys.exit(1)

        if total_expected is None:
            total_expected = pagination.get("total", len(results))
            print(f"Всего объявлений на сайте: {total_expected}")

        if not results:
            print("Пустая страница -- останавливаюсь.")
            break

        all_items.extend(results)
        print(f"  -> получено {len(results)} объявлений, всего собрано: {len(all_items)}")

        if len(all_items) >= total_expected:
            break

        page += 1
        time.sleep(1)

        if page > 20:
            print("Слишком много страниц, останавливаюсь на всякий случай.")
            break

    print(f"\nТеперь получаю описания для {len(all_items)} объявлений (по одному запросу на каждое)...")

    for i, item in enumerate(all_items, 1):
        hash_id = str(item.get("id"))
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

        print(f"[{i}/{len(all_items)}] Получаю описание для {hash_id}...")
        detail_data = fetch_estate_detail_json(hash_id)
        description = extract_description(detail_data)

        try:
            conn.execute(
                """INSERT INTO seen_listings
                   (hash_id, name, city_part, street, price_czk, area_m2,
                    distance_to_station_km, link, description, first_seen_at, is_active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (hash_id, name, city_part, street, price, area,
                 distance_km, link, description, now),
            )
            total_inserted += 1
        except sqlite3.IntegrityError:
            pass

        conn.commit()
        time.sleep(1)  # не долбим сайт слишком часто

    conn.close()

    print(f"\nГотово. В базе {DB_FILE}: {total_inserted} из {total_expected} объявлений, с описаниями.")


if __name__ == "__main__":
    main()