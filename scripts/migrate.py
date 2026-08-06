import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "events.db")

if not os.path.exists(DB_PATH):
    print("DB does not exist at", DB_PATH)
    exit(1)

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()
cur.execute("CREATE INDEX IF NOT EXISTS idx_events_store_ts ON events(store_id, timestamp)")
conn.commit()
print("migrations applied")
