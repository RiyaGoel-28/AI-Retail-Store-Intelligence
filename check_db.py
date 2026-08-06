import sqlite3
conn = sqlite3.connect('data/events.db')
r = conn.execute("SELECT DISTINCT event_type FROM events WHERE store_id='STORE_BLR_002'").fetchall()
for x in r:
    print(x[0])
