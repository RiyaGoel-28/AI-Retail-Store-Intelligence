"""
Database initialization for SQLite event storage.
Stores all event fields as indexed columns + full JSON payload.
"""
import sqlite3
import os

DB_PATH = os.path.join("data", "events.db")


def init_db():
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            store_id TEXT,
            camera_id TEXT,
            visitor_id TEXT,
            event_type TEXT,
            timestamp TEXT,
            zone_id TEXT,
            zone_name TEXT,
            is_staff INTEGER DEFAULT 0,
            payload TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_store_id ON events(store_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_event_type ON events(event_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON events(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_visitor_id ON events(visitor_id)")
    conn.commit()
    conn.close()