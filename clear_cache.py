import sqlite3, os
db = os.environ.get("DB_PATH", "/data/kara_data.db")
c = sqlite3.connect(db)
c.execute("DELETE FROM vol_cache")
c.commit()
print(f"CLEARED vol_cache from {db}")
