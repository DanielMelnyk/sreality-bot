"""
ТЕСТОВЫЙ скрипт: симулирует "пропажу" объявления, не трогая реальные
данные с sreality.cz. Берёт одну активную запись из базы (у которой уже
есть сохранённые фото и описание) и дублирует её под фейковым ID,
которого точно не будет в реальной выдаче сайта.

После этого при запуске check_new_listings.py бот увидит, что этот
фейковый ID "пропал" (он есть в базе как активный, но не встречается в
текущей выдаче sreality.cz) и пришлёт уведомление о пропаже -- ровно
так же, как это будет происходить с настоящими объявлениями.

ЗАПУСК:
    python test_simulate_removed.py
    python check_new_listings.py   <-- вот тут придёт тестовое уведомление
"""

import sqlite3
import time

DB_FILE = "sreality_seen.db"
FAKE_PREFIX = "TESTREMOVED_"


def main():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("""
        SELECT hash_id, name, city_part, street, price_czk, area_m2,
               distance_to_station_km, link, description, local_photos_json
        FROM seen_listings
        WHERE is_active = 1
          AND local_photos_json IS NOT NULL AND local_photos_json != '[]'
          AND description IS NOT NULL AND description != ''
        LIMIT 1
    """)
    row = cur.fetchone()

    if row is None:
        print("Не нашёл подходящую активную запись (с фото и описанием) для теста.")
        conn.close()
        return

    (hash_id, name, city_part, street, price, area,
     distance_km, link, description, local_photos_json) = row

    fake_id = f"{FAKE_PREFIX}{hash_id}"

    cur.execute("SELECT 1 FROM seen_listings WHERE hash_id = ?", (fake_id,))
    if cur.fetchone():
        print(f"Тестовая запись {fake_id} уже существует -- ничего не делаю.")
        print("Просто запусти check_new_listings.py, если ещё не запускал после прошлого теста.")
        conn.close()
        return

    now = int(time.time())
    cur.execute(
        """INSERT INTO seen_listings
           (hash_id, name, city_part, street, price_czk, area_m2,
            distance_to_station_km, link, description, first_seen_at,
            is_active, local_photos_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
        (fake_id, name, city_part, street, price, area,
         distance_km, link, description, now, local_photos_json),
    )
    conn.commit()
    conn.close()

    print(f"Создал тестовую запись: {fake_id} (копия {hash_id} -- {name})")
    print("\nТеперь запусти:  python check_new_listings.py")
    print("Бот должен увидеть эту запись как 'пропавшую' и прислать уведомление в Telegram")
    print("с сохранёнными фото и полным описанием.")
    print("\nПосле теста можно почистить: python test_simulate_removed_cleanup.py")


if __name__ == "__main__":
    main()