import os
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from threading import Lock

from models.schemas import User, UserConfig, BotMode, Position, TradeSignal
import config

try:
    from cryptography.fernet import Fernet
except ImportError:
    Fernet = None

log = logging.getLogger("kara.db")

_fernet = None
if Fernet and hasattr(config, "FERNET_KEY") and config.FERNET_KEY:
    try:
        _fernet = Fernet(config.FERNET_KEY.encode())
    except Exception as e:
        log.error(f"Invalid FERNET_KEY: {e}")


class UserDB:
    def __init__(self, file_path: str = None, db_path: str = None):
        self.file_path = file_path or config.USER_DB_PATH
        self.db_path = db_path or config.DB_PATH
        self._lock = Lock()
        self.users: Dict[str, User] = {}
        
        # Ensure data dir exists
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        
        self.load()
        self._init_sqlite()

    def _get_conn(self):
        if not hasattr(self, '_shared_conn'):
            self._shared_conn = sqlite3.connect(self.db_path, check_same_thread=False)
        return self._shared_conn

    def _init_sqlite(self):
        """Initialize SQLite tables for high-frequency or structured persistence."""
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                
                # 1. Volatility Cache (Bug 2 Fix)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS vol_cache (
                        asset         TEXT PRIMARY KEY,
                        regime        TEXT,
                        realized_vol  REAL,
                        trend         REAL,
                        cached_at     REAL
                    )
                """)
                
                # 2. Paper Positions (Bug 1 Fix - Survivability)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS paper_positions (
                        pos_id    TEXT PRIMARY KEY,
                        chat_id   TEXT,
                        asset     TEXT,
                        data      TEXT,
                        opened_at REAL
                    )
                """)
                
                # 3. Paper State (Balance Persistence)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS paper_state (
                        chat_id    TEXT PRIMARY KEY,
                        balance    REAL,
                        equity     REAL,
                        updated_at REAL
                    )
                """)

                # 4. Signals History (Plan v17)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS signals_history (
                        sig_id     TEXT PRIMARY KEY,
                        asset      TEXT,
                        side       TEXT,
                        score      INTEGER,
                        price      REAL,
                        data       TEXT,
                        created_at REAL
                    )
                """)
                
                # 5. Risk State
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS risk_state (
                        chat_id    TEXT PRIMARY KEY,
                        data       TEXT,
                        updated_at REAL
                    )
                """)
                
                conn.commit()
                # conn.close() # Connection pooling applied
                log.info(f"✓ SQLite database initialized at {self.db_path}")
            except Exception as e:
                log.error(f"Failed to init SQLite: {e}")

    # ── RISK STATE ────────────────────────────────────────────────────

    def save_risk_state(self, chat_id: str, data: dict):
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO risk_state (chat_id, data, updated_at)
                    VALUES (?, ?, ?)
                """, (chat_id, json.dumps(data), datetime.now(timezone.utc).timestamp()))
                conn.commit()
                # conn.close() # Connection pooling applied
            except Exception as e:
                log.error(f"Failed to save risk state for {chat_id}: {e}")

    def load_risk_state(self, chat_id: str) -> Optional[dict]:
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute("SELECT data FROM risk_state WHERE chat_id = ?", (chat_id,))
                row = cursor.fetchone()
                # conn.close() # Connection pooling applied
                if row:
                    return json.loads(row[0])
            except Exception as e:
                log.error(f"Failed to load risk state for {chat_id}: {e}")
        return None

    # ── VOLATILITY CACHE ──────────────────────────────────────────────

    def save_vol_cache(self, asset: str, regime: str, realized_vol: float, trend: float):
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT OR REPLACE INTO vol_cache VALUES (?, ?, ?, ?, ?)",
                    (asset, regime, realized_vol, trend, datetime.now(timezone.utc).timestamp())
                )
                conn.commit()
                # conn.close() # Connection pooling applied
            except Exception as e:
                log.error(f"Error saving vol_cache for {asset}: {e}")

    def get_vol_cache(self, asset: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM vol_cache WHERE asset = ?", (asset,))
                row = cursor.fetchone()
                # conn.close() # Connection pooling applied
                if row:
                    return {
                        "regime": row[1],
                        "realized_vol": row[2],
                        "trend": row[3],
                        "cached_at": row[4]
                    }
            except Exception as e:
                log.error(f"Error loading vol_cache for {asset}: {e}")
        return None

    # ── SIGNALS HISTORY (Plan v17) ────────────────────────────────────

    def save_signal(self, sig: TradeSignal):
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                # model_dump is pydantic v2
                sig_data = sig.model_dump_json() if hasattr(sig, 'model_dump_json') else json.dumps(sig.dict())
                cursor.execute(
                    "INSERT OR REPLACE INTO signals_history VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        sig.signal_id, 
                        sig.asset, 
                        sig.side.value, 
                        sig.score, 
                        sig.entry_price, 
                        sig_data, 
                        sig.timestamp.timestamp()
                    )
                )
                conn.commit()
                # conn.close() # Connection pooling applied
            except Exception as e:
                log.error(f"Error saving signal {sig.signal_id}: {e}")

    def load_signals(self, limit: int = 20) -> List[TradeSignal]:
        signals = []
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT data FROM signals_history ORDER BY created_at DESC LIMIT ?", 
                    (limit,)
                )
                rows = cursor.fetchall()
                # conn.close() # Connection pooling applied
                from models.schemas import TradeSignal
                for (data_str,) in rows:
                    signals.append(TradeSignal(**json.loads(data_str)))
            except Exception as e:
                log.error(f"Error loading signals: {e}")
        return signals

    # ── PAPER POSITIONS & STATE ───────────────────────────────────────

    def save_paper_position(self, chat_id: str, pos: Position):
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                # model_dump is pydantic v2
                pos_data = pos.model_dump_json() if hasattr(pos, 'model_dump_json') else json.dumps(pos.dict())
                cursor.execute(
                    "INSERT OR REPLACE INTO paper_positions VALUES (?, ?, ?, ?, ?)",
                    (pos.position_id, str(chat_id), pos.asset, pos_data, pos.opened_at.timestamp())
                )
                conn.commit()
                # conn.close() # Connection pooling applied
            except Exception as e:
                log.error(f"Error saving position {pos.position_id}: {e}")

    def remove_paper_position(self, pos_id: str):
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute("DELETE FROM paper_positions WHERE pos_id = ?", (pos_id,))
                conn.commit()
                # conn.close() # Connection pooling applied
            except Exception as e:
                log.error(f"Error removing position {pos_id}: {e}")

    def load_paper_positions(self, chat_id: str) -> List[Position]:
        positions = []
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute("SELECT data FROM paper_positions WHERE chat_id = ?", (str(chat_id),))
                rows = cursor.fetchall()
                # conn.close() # Connection pooling applied
                for (data_str,) in rows:
                    positions.append(Position(**json.loads(data_str)))
            except Exception as e:
                log.error(f"Error loading positions for {chat_id}: {e}")
        return positions

    def save_paper_state(self, chat_id: str, balance: float, equity: float):
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT OR REPLACE INTO paper_state VALUES (?, ?, ?, ?)",
                    (str(chat_id), balance, equity, datetime.now(timezone.utc).timestamp())
                )
                conn.commit()
                # conn.close() # Connection pooling applied
            except Exception as e:
                log.error(f"Error saving paper state for {chat_id}: {e}")

    def load_paper_state(self, chat_id: str) -> Optional[Dict[str, float]]:
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute("SELECT balance, equity FROM paper_state WHERE chat_id = ?", (str(chat_id),))
                row = cursor.fetchone()
                # conn.close() # Connection pooling applied
                if row:
                    return {"balance": row[0], "equity": row[1]}
            except Exception as e:
                log.error(f"Error loading paper state for {chat_id}: {e}")
        return None

    # ── USER DB (JSON fallback) ───────────────────────────────────────

    def load(self):
        with self._lock:
            if not os.path.exists(self.file_path):
                self.users = {}
                return
                
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for chat_id, u_data in data.items():
                        user_obj = User(**u_data)
                        if user_obj.hl_agent_secret and _fernet:
                            try:
                                user_obj.hl_agent_secret = _fernet.decrypt(user_obj.hl_agent_secret.encode()).decode()
                            except Exception:
                                pass # Perhaps it was plaintext, keep it
                        self.users[chat_id] = user_obj
                log.info(f"Loaded {len(self.users)} users from JSON database.")
            except Exception as e:
                log.error(f"Failed to load user DB: {e}")
                
    def save(self):
        with self._lock:
            try:
                with open(self.file_path, "w", encoding="utf-8") as f:
                    # serialize using pydantic
                    data = {}
                    for k, v in self.users.items():
                        udict = v.model_dump() if hasattr(v, 'model_dump') else v.dict()
                        # Encrypt secret before saving
                        if udict.get("hl_agent_secret") and _fernet:
                            try:
                                udict["hl_agent_secret"] = _fernet.encrypt(udict["hl_agent_secret"].encode()).decode()
                            except Exception:
                                pass
                        data[k] = udict
                    # handle datetime parsing issues in basic json
                    json.dump(data, f, default=str, indent=2)
            except Exception as e:
                log.error(f"Failed to save user DB: {e}")

    def get_user(self, chat_id: str) -> Optional[User]:
        return self.users.get(str(chat_id))
        
    def get_all_users(self) -> List[User]:
        return list(self.users.values())

    def update_user(self, user: User):
        self.users[user.chat_id] = user
        self.save()

    def create_user(self, chat_id: str, username: str, init_usd: float) -> User:
        user = User(
            chat_id=str(chat_id),
            username=username,
            paper_balance_usd=init_usd,
            config=UserConfig(
                trading_mode="standard",
                bot_mode=BotMode.PAPER,
                risk_pct=0.02
            )
        )
        self.users[str(chat_id)] = user
        self.save()
        return user

# Global instance
user_db = UserDB()

