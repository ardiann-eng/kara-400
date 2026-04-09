import sqlite3
import json

def run():
    conn = sqlite3.connect('kara_data.db')
    cursor = conn.cursor()
    
    # Check tables
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = cursor.fetchall()
    print("Tables:", tables)

    # Check paper_positions
    try:
        cursor.execute("SELECT count(*) FROM paper_positions")
        print("Open paper_positions:", cursor.fetchone()[0])
    except:
        pass
        
    # Check history_snapshots
    try:
        cursor.execute("SELECT count(*) FROM history_snapshots")
        print("history_snapshots count:", cursor.fetchone()[0])
    except:
        pass
        
    # Check signals_history
    try:
        cursor.execute("SELECT count(*) FROM signals_history")
        print("signals_history count:", cursor.fetchone()[0])
    except:
        pass

    conn.close()

if __name__ == '__main__':
    run()
