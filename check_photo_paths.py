"""
Проверяет, есть ли в базе пути к фото с обратным слэшем (Windows-стиль)
-- такие пути не находятся на Linux (GitHub Actions), только на Windows.

ЗАПУСК:
    python check_photo_paths.py
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

total = 0
with_backslash = 0

for hash_id, photos_json in cur.fetchall():
    total += 1
    photos = json.loads(photos_json)
    if any("\\" in p for p in photos):
        with_backslash += 1
        print(f"[{hash_id}] ПРОБЛЕМНЫЕ пути: {photos}")

conn.close()

print(f"\nВсего записей с фото: {total}")
print(f"Из них с обратным слэшем (сломаются на Linux/GitHub Actions): {with_backslash}")