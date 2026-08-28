"""
Убирает тестовую запись(и), созданную test_simulate_removed.py
(и сама себя пометит is_active=0 после того как check_new_listings.py
её обработает -- этот скрипт просто полностью удаляет её из базы,
чтобы не засорять историю).

ЗАПУСК:
    python test_simulate_removed_cleanup.py
"""

import sqlite3

DB_FILE = "sreality_seen.db"
FAKE_PREFIX = "TESTREMOVED_"

conn = sqlite3.connect(DB_FILE)
cur = conn.cursor()

cur.execute("SELECT hash_id FROM seen_listings WHERE hash_id LIKE ?", (f"{FAKE_PREFIX}%",))
rows = cur.fetchall()

if not rows:
    print("Тестовых записей не найдено, нечего чистить.")
else:
    for (hash_id,) in rows:
        cur.execute("DELETE FROM seen_listings WHERE hash_id = ?", (hash_id,))
        print(f"Удалил тестовую запись: {hash_id}")
    conn.commit()
    print(f"\nГотово, удалено записей: {len(rows)}.")

conn.close()