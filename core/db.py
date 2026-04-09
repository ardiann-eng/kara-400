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
if Fernet and getattr(config, "FERNET_KEY", None):
    try:
        _fernet = Fernet(config.FERNET_KEY.encode())
    except Exception as e:
        log.error(f"❌ Invalid FERNET_KEY: {e}. Secure storage will be disabled.")


class UserDB:
    def __init__(self, file_path: str = None, db_path: str = None):
        self.file_path = file_path or config.USER_DB_PATH
        self.db_path = db_path or config.DB_PATH
        self._lock = Lock()
        self.users: Dict[str, User] = {}
        
        # Ensure data dir exists
        file_dir = os.path.dirname(os.path.abspath(self.file_path))
        if file_dir:
            os.makedirs(file_dir, exist_ok=True)
        db_dir = os.path.dirname(os.path.abspath(self.db_path))
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        
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

                # 6. History Snapshots (For Charts)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS history_snapshots (
                        timestamp    REAL PRIMARY KEY,
                        total_users  INTEGER,
                        active_users INTEGER,
                        global_pnl   REAL,
                        global_equity REAL
                    )
                """)

                # 7. Meta pattern stats (Outcome-based score learning)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS meta_pattern_stats (
                        pattern_key TEXT PRIMARY KEY,
                        winrate_ema REAL,
                        pnl_ema     REAL,
                        samples     INTEGER,
                        updated_at  REAL
                    )
                """)
                
                # 8. OI Snapshots Cache (Priority 2 Fix - Amnesia)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS oi_snapshots (
                        asset TEXT PRIMARY KEY,
                        snapshots_json TEXT,
                        updated_at REAL
                    )
                """)
                
                conn.commit()
                # conn.close() # Connection pooling applied
                log.info(f"✓ SQLite database initialized at {self.db_path}")
            except Exception as e:
                log.error(f"Failed to init SQLite: {e}")

    # ── OI SNAPSHOTS ──────────────────────────────────────────────────

    def save_oi_snapshots_batch(self, snapshots_dict: Dict[str, list]):
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                now_ts = datetime.now(timezone.utc).timestamp()
                rows = []
                for asset, snaps in snapshots_dict.items():
                    rows.append((asset, json.dumps(snaps), now_ts))
                cursor.executemany(
                    "INSERT OR REPLACE INTO oi_snapshots VALUES (?, ?, ?)",
                    rows
                )
                conn.commit()
            except Exception as e:
                log.error(f"Error saving oi_snapshots batch: {e}")

    def load_all_oi_snapshots(self) -> Dict[str, list]:
        snapshots_dict = {}
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute("SELECT asset, snapshots_json FROM oi_snapshots")
                rows = cursor.fetchall()
                for row in rows:
                    try:
                        asset = row[0]
                        snapshots = json.loads(row[1])
                        snapshots_dict[asset] = snapshots
                    except Exception as parse_e:
                        log.debug(f"Failed to parse oi snapshot for {row[0]}: {parse_e}")
            except Exception as e:
                log.error(f"Error loading all oi_snapshots: {e}")
        return snapshots_dict

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

    def get_signal_by_id(self, signal_id: str) -> Optional[TradeSignal]:
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute("SELECT data FROM signals_history WHERE sig_id = ?", (signal_id,))
                row = cursor.fetchone()
                if row:
                    from models.schemas import TradeSignal
                    return TradeSignal(**json.loads(row[0]))
            except Exception as e:
                log.error(f"Error loading signal by id {signal_id}: {e}")
        return None

    def update_meta_pattern_outcome(self, pattern_key: str, pnl_usd: float, alpha: float = 0.20):
        """
        Rolling pattern outcome stats:
        - winrate_ema in [0,1]
        - pnl_ema (USD, smoothed)
        """
        if not pattern_key:
            return
        win = 1.0 if pnl_usd > 0 else 0.0
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT winrate_ema, pnl_ema, samples FROM meta_pattern_stats WHERE pattern_key = ?",
                    (pattern_key,)
                )
                row = cursor.fetchone()
                if row:
                    old_wr, old_pnl, old_n = float(row[0]), float(row[1]), int(row[2])
                    wr = (1 - alpha) * old_wr + alpha * win
                    pnl = (1 - alpha) * old_pnl + alpha * float(pnl_usd)
                    n = old_n + 1
                else:
                    wr = win
                    pnl = float(pnl_usd)
                    n = 1
                cursor.execute(
                    "INSERT OR REPLACE INTO meta_pattern_stats (pattern_key, winrate_ema, pnl_ema, samples, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (pattern_key, wr, pnl, n, datetime.now(timezone.utc).timestamp())
                )
                conn.commit()
            except Exception as e:
                log.error(f"Error updating meta pattern stats for {pattern_key}: {e}")

    def get_meta_pattern_stats(self, pattern_key: str) -> Optional[Dict[str, Any]]:
        if not pattern_key:
            return None
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT winrate_ema, pnl_ema, samples, updated_at FROM meta_pattern_stats WHERE pattern_key = ?",
                    (pattern_key,)
                )
                row = cursor.fetchone()
                if row:
                    return {
                        "winrate_ema": float(row[0]),
                        "pnl_ema": float(row[1]),
                        "samples": int(row[2]),
                        "updated_at": float(row[3]),
                    }
            except Exception as e:
                log.error(f"Error loading meta pattern stats for {pattern_key}: {e}")
        return None

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

    def clear_paper_positions(self, chat_id: str):
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute("DELETE FROM paper_positions WHERE chat_id = ?", (str(chat_id),))
                conn.commit()
            except Exception as e:
                log.error(f"Error clearing paper positions for {chat_id}: {e}")

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

    def clear_paper_state(self, chat_id: str):
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute("DELETE FROM paper_state WHERE chat_id = ?", (str(chat_id),))
                conn.commit()
            except Exception as e:
                log.error(f"Error clearing paper state for {chat_id}: {e}")

    def clear_risk_state(self, chat_id: str):
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute("DELETE FROM risk_state WHERE chat_id = ?", (str(chat_id),))
                conn.commit()
            except Exception as e:
                log.error(f"Error clearing risk state for {chat_id}: {e}")

    # ── HISTORY SNAPSHOTS (For Charts) ────────────────────────────────

    def save_snapshot(self, total_users: int, active_users: int, pnl: float, equity: float):
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO history_snapshots VALUES (?, ?, ?, ?, ?)",
                    (datetime.now(timezone.utc).timestamp(), total_users, active_users, pnl, equity)
                )
                conn.commit()
            except Exception as e:
                log.error(f"Error saving snapshot: {e}")

    def load_history(self, days: int = 7) -> List[Dict[str, Any]]:
        history = []
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
                cursor.execute(
                    "SELECT * FROM history_snapshots WHERE timestamp > ? ORDER BY timestamp ASC",
                    (cutoff,)
                )
                rows = cursor.fetchall()
                for r in rows:
                    history.append({
                        "time": float(r[0]),
                        "total_users": int(r[1]),
                        "active_users": int(r[2]),
                        "global_pnl": float(r[3]),
                        "global_equity": float(r[4])
                    })
            except Exception as e:
                log.error(f"Error loading history: {e}")
        return history

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
                                # First we check if it is already encrypted (starts with gAAAA...)
                                secret = user_obj.hl_agent_secret
                                if secret.startswith("gAAAA"):
                                    user_obj.hl_agent_secret = _fernet.decrypt(secret.encode()).decode()
                            except Exception as e:
                                log.debug(f"Failed to decrypt secret for {chat_id}: {e}")
                                # keep as is if decryption fails (might be plaintext or wrong key)
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
                                # Only encrypt if not already encrypted
                                secret = udict["hl_agent_secret"]
                                if not secret.startswith("gAAAA"):
                                    udict["hl_agent_secret"] = _fernet.encrypt(secret.encode()).decode()
                            except Exception as e:
                                log.error(f"Failed to encrypt secret for {k}: {e}")
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

