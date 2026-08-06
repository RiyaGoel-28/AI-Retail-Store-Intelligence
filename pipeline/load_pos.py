"""
Load POS transactions from actual CSV format into SQLite.
CSV columns: order_id, order_date (DD-MM-YYYY), order_time, store_id, product_id, brand_name, total_amount
"""
import csv
import sqlite3
import os
import sys


def load_pos(csv_path: str, db_path: str = "data/store_intelligence.db"):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pos_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT,
            order_date TEXT,
            order_time TEXT,
            store_id TEXT,
            product_id TEXT,
            brand_name TEXT,
            total_amount REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pos_store ON pos_transactions(store_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pos_order ON pos_transactions(order_id)")
    conn.commit()

    inserted = 0
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                conn.execute("""
                    INSERT INTO pos_transactions (order_id, order_date, order_time, store_id, product_id, brand_name, total_amount)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    row.get("order_id", ""),
                    row.get("order_date", ""),
                    row.get("order_time", ""),
                    row.get("store_id", ""),
                    row.get("product_id", ""),
                    row.get("brand_name", ""),
                    float(row.get("total_amount", 0)),
                ))
                inserted += 1
            except Exception as e:
                print(f"  ⚠️ Skipping row: {e}")

    conn.commit()
    conn.close()
    print(f"✅ Loaded {inserted} POS line items into {db_path}")


if __name__ == "__main__":
    csv_file = sys.argv[1] if len(sys.argv) > 1 else "data/pos_transactions.csv"
    load_pos(csv_file)