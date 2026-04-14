import os
import json
import sqlite3
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kara.reset")

# Paths (adjust if running from root)
DB_PATH = "data/kara_v2.db"
USERS_JSON = "data/users.json"
DEFAULT_BALANCE_USD = 1000000.0 / 16000.0  # Rp 1.000.000 / 16.000

def hard_reset():
    log.info("🚀 Starting Hard Reset of all user balances and positions...")

    # 1. Clear SQLite tables
    if os.path.exists(DB_PATH):
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            # Wipe positions
            cursor.execute("DELETE FROM paper_positions")
            log.info(f"✓ Cleared paper_positions ({cursor.rowcount} rows)")
            
            # Wipe balances/equity states
            cursor.execute("DELETE FROM paper_state")
            log.info(f"✓ Cleared paper_state ({cursor.rowcount} rows)")
            
            # Wipe PnL history
            cursor.execute("DELETE FROM daily_pnl_history")
            log.info(f"✓ Cleared daily_pnl_history ({cursor.rowcount} rows)")
            
            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f"❌ Error resetting SQLite: {e}")
    else:
        log.warning(f"⚠️ SQLite DB not found at {DB_PATH}")

    # 2. Reset users.json Legacy Storage
    if os.path.exists(USERS_JSON):
        try:
            with open(USERS_JSON, 'r') as f:
                users = json.load(f)
            
            reset_count = 0
            for chat_id, user_data in users.items():
                user_data['paper_balance_usd'] = DEFAULT_BALANCE_USD
                user_data['wallet_authorized'] = False # Forafety, force re-authorization check if desired
                # Keep hl_agent_address/secret so they don't have to re-setup wallets, 
                # but ensure state is clean.
                reset_count += 1
            
            with open(USERS_JSON, 'w') as f:
                json.dump(users, f, indent=4)
            
            log.info(f"✓ Reset {reset_count} users in users.json to default balance (${DEFAULT_BALANCE_USD:.2f})")
        except Exception as e:
            log.error(f"❌ Error resetting users.json: {e}")
    else:
        log.warning(f"⚠️ users.json not found at {USERS_JSON}")

    log.info("✨ Hard Reset Complete. All users are now back to fresh starts.")

if __name__ == "__main__":
    hard_reset()
