"""
Одноразовый скрипт: нормализует все пути к фото в базе, заменяя
обратные слэши (Windows-стиль) на прямые -- чтобы пути одинаково
находились и на Windows (локально), и на Linux (GitHub Actions).

ЗАПУСК:
    python normalize_photo_paths.py
"""

import sqlite3
import json

DB_FILE = "sreality_seen.db"

conn = sqlite3.connect(DB_FILE)
cur = conn.cursor()

cur.execute("""
    SELECT hash_id, local_photos_json FROM seen_listings
    WHERE local_photos_json IS NOT NULL AND local_photos_json != '[]'
""")
rows = cur.fetchall()

updated = 0
for hash_id, photos_json in rows:
    photos = json.loads(photos_json)
    normalized = [p.replace("\\", "/") for p in photos]
    if normalized != photos:
        cur.execute(
            "UPDATE seen_listings SET local_photos_json = ? WHERE hash_id = ?",
            (json.dumps(normalized, ensure_ascii=False), hash_id),
        )
        updated += 1
        print(f"[{hash_id}] {photos} -> {normalized}")

conn.commit()
conn.close()

print(f"\nГотово. Нормализовано записей: {updated}.")