import os
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from threading import Lock

from models.schemas import User, UserConfig, BotMode, Position, TradeSignal
import config

log = logging.getLogger("kara.db")

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

    def _init_sqlite(self):
        """Initialize SQLite tables for high-frequency or structured persistence."""
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path)
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
                
                conn.commit()
                conn.close()
                log.info(f"✓ SQLite database initialized at {self.db_path}")
            except Exception as e:
                log.error(f"Failed to init SQLite: {e}")

    # ── VOLATILITY CACHE ──────────────────────────────────────────────

    def save_vol_cache(self, asset: str, regime: str, realized_vol: float, trend: float):
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT OR REPLACE INTO vol_cache VALUES (?, ?, ?, ?, ?)",
                    (asset, regime, realized_vol, trend, datetime.now(timezone.utc).timestamp())
                )
                conn.commit()
                conn.close()
            except Exception as e:
                log.error(f"Error saving vol_cache for {asset}: {e}")

    def get_vol_cache(self, asset: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM vol_cache WHERE asset = ?", (asset,))
                row = cursor.fetchone()
                conn.close()
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
                conn = sqlite3.connect(self.db_path)
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
                conn.close()
            except Exception as e:
                log.error(f"Error saving signal {sig.signal_id}: {e}")

    def load_signals(self, limit: int = 20) -> List[TradeSignal]:
        signals = []
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT data FROM signals_history ORDER BY created_at DESC LIMIT ?", 
                    (limit,)
                )
                rows = cursor.fetchall()
                conn.close()
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
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                # model_dump is pydantic v2
                pos_data = pos.model_dump_json() if hasattr(pos, 'model_dump_json') else json.dumps(pos.dict())
                cursor.execute(
                    "INSERT OR REPLACE INTO paper_positions VALUES (?, ?, ?, ?, ?)",
                    (pos.position_id, str(chat_id), pos.asset, pos_data, pos.opened_at.timestamp())
                )
                conn.commit()
                conn.close()
            except Exception as e:
                log.error(f"Error saving position {pos.position_id}: {e}")

    def remove_paper_position(self, pos_id: str):
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute("DELETE FROM paper_positions WHERE pos_id = ?", (pos_id,))
                conn.commit()
                conn.close()
            except Exception as e:
                log.error(f"Error removing position {pos_id}: {e}")

    def load_paper_positions(self, chat_id: str) -> List[Position]:
        positions = []
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT data FROM paper_positions WHERE chat_id = ?", (str(chat_id),))
                rows = cursor.fetchall()
                conn.close()
                for (data_str,) in rows:
                    positions.append(Position(**json.loads(data_str)))
            except Exception as e:
                log.error(f"Error loading positions for {chat_id}: {e}")
        return positions

    def save_paper_state(self, chat_id: str, balance: float, equity: float):
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT OR REPLACE INTO paper_state VALUES (?, ?, ?, ?)",
                    (str(chat_id), balance, equity, datetime.now(timezone.utc).timestamp())
                )
                conn.commit()
                conn.close()
            except Exception as e:
                log.error(f"Error saving paper state for {chat_id}: {e}")

    def load_paper_state(self, chat_id: str) -> Optional[Dict[str, float]]:
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT balance, equity FROM paper_state WHERE chat_id = ?", (str(chat_id),))
                row = cursor.fetchone()
                conn.close()
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
                        self.users[chat_id] = User(**u_data)
                log.info(f"Loaded {len(self.users)} users from JSON database.")
            except Exception as e:
                log.error(f"Failed to load user DB: {e}")
                
    def save(self):
        with self._lock:
            try:
                with open(self.file_path, "w", encoding="utf-8") as f:
                    # serialize using pydantic
                    data = {k: v.model_dump() if hasattr(v, 'model_dump') else v.dict() for k, v in self.users.items()}
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
                risk_pct=0.02,
                max_positions=5
            )
        )
        self.users[str(chat_id)] = user
        self.save()
        return user

# Global instance
user_db = UserDB()

