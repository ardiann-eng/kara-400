import os
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from threading import RLock

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
        log.info("🔐 Fernet encryption active — agent secrets will be encrypted at rest.")
    except Exception as e:
        log.error(f"❌ Invalid FERNET_KEY: {e}. Secure storage will be disabled.")
else:
    if getattr(config, "TRADE_MODE", "paper") == "live":
        log.critical(
            "🚨 LIVE MODE WITHOUT FERNET_KEY — agent wallet private keys are stored "
            "in PLAINTEXT in users.json. Set HL_FERNET_KEY env var immediately. "
            "Generate with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    else:
        log.warning(
            "⚠️  No FERNET_KEY set — agent wallet secrets will NOT be encrypted. "
            "Set HL_FERNET_KEY before enabling Live Mode."
        )


class UserDB:
    def __init__(self, file_path: str = None, db_path: str = None):
        self.file_path = file_path or config.USER_DB_PATH
        self.db_path = db_path or config.DB_PATH
        self._lock = RLock()
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

    def save_weak_confirmation_event(
        self,
        event_id: str,
        asset: str,
        side: str,
        status: str,
        signal_price: float,
        observed_price: Optional[float],
        score: int,
        armed_at: float,
        decided_at: float,
    ):
        with self._lock:
            try:
                conn = self._get_conn()
                conn.execute(
                    """
                    INSERT OR REPLACE INTO weak_confirmation_events
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        asset,
                        side,
                        status,
                        signal_price,
                        observed_price,
                        score,
                        armed_at,
                        decided_at,
                    ),
                )
                conn.commit()
            except Exception as exc:
                log.error(f"Failed to save weak confirmation event: {exc}")

    def save_weak_confirmation_outcome(
        self,
        event_id: str,
        asset: str,
        side: str,
        signal_price: float,
        observed_price: float,
        metrics: dict,
        completed_at: float,
    ):
        with self._lock:
            try:
                conn = self._get_conn()
                conn.execute(
                    """
                    INSERT OR REPLACE INTO weak_confirmation_outcomes (
                        event_id, asset, side, signal_price, observed_price,
                        mfe_pct, mae_pct, final_return_pct, tp1_hit, tp2_hit,
                        sl_hit, completed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        asset,
                        side,
                        signal_price,
                        observed_price,
                        metrics["mfe_pct"],
                        metrics["mae_pct"],
                        metrics["final_return_pct"],
                        int(metrics["tp1_hit"]),
                        int(metrics["tp2_hit"]),
                        int(metrics["sl_hit"]),
                        completed_at,
                    ),
                )
                conn.commit()
            except Exception as exc:
                log.error(f"Failed to save weak confirmation outcome: {exc}")

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

                # 10. Trade History (REFIX: Individual records for export/journal)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS trade_history (
                        trade_id   TEXT PRIMARY KEY,
                        chat_id    TEXT,
                        asset      TEXT,
                        side       TEXT,
                        pnl_usd    REAL,
                        pnl_pct    REAL,
                        data       TEXT,
                        created_at REAL
                    )
                """)

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS execution_candidates (
                        candidate_id TEXT PRIMARY KEY,
                        chat_id TEXT NOT NULL,
                        asset TEXT NOT NULL,
                        side TEXT,
                        status TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        execution_environment TEXT NOT NULL,
                        data TEXT NOT NULL,
                        created_at REAL NOT NULL
                    )
                """)

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS bybit_positions (
                        position_id TEXT PRIMARY KEY,
                        chat_id     TEXT NOT NULL,
                        symbol      TEXT NOT NULL,
                        side        TEXT NOT NULL,
                        live_status TEXT NOT NULL,
                        entry_order_link_id TEXT,
                        data        TEXT NOT NULL,
                        updated_at  REAL NOT NULL
                    )
                """)

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS weak_confirmation_events (
                        event_id TEXT PRIMARY KEY,
                        asset TEXT NOT NULL,
                        side TEXT NOT NULL,
                        status TEXT NOT NULL,
                        signal_price REAL NOT NULL,
                        observed_price REAL,
                        score INTEGER,
                        armed_at REAL NOT NULL,
                        decided_at REAL NOT NULL
                    )
                """)

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS weak_confirmation_outcomes (
                        event_id TEXT PRIMARY KEY,
                        asset TEXT NOT NULL,
                        side TEXT NOT NULL,
                        signal_price REAL NOT NULL,
                        observed_price REAL NOT NULL,
                        mfe_pct REAL NOT NULL,
                        mae_pct REAL NOT NULL,
                        final_return_pct REAL NOT NULL,
                        tp1_hit INTEGER NOT NULL,
                        tp2_hit INTEGER NOT NULL,
                        sl_hit INTEGER NOT NULL,
                        completed_at REAL NOT NULL
                    )
                """)
                outcome_columns = {
                    row[1] for row in cursor.execute("PRAGMA table_info(weak_confirmation_outcomes)")
                }
                if "tp2_hit" not in outcome_columns:
                    cursor.execute(
                        "ALTER TABLE weak_confirmation_outcomes "
                        "ADD COLUMN tp2_hit INTEGER NOT NULL DEFAULT 0"
                    )

                conn.commit()

                # Startup diagnostic — confirms persistent storage is working
                cursor.execute("SELECT COUNT(*) FROM trade_history")
                trade_count = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM paper_positions")
                pos_count = cursor.fetchone()[0]
                log.info(
                    f"✓ SQLite initialized at {self.db_path} "
                    f"| trades={trade_count} | positions={pos_count}"
                )
                if trade_count == 0 and os.path.getsize(self.db_path) < 50_000:
                    log.warning(
                        "⚠️  trade_history is EMPTY on startup. "
                        "If trades existed before, the persistent volume may not be mounted correctly. "
                        f"DB path: {self.db_path}"
                    )
            except Exception as e:
                log.error(f"Failed to init SQLite: {e}")

    def hard_reset_all_data(self) -> dict:
        """
        Irreversible Option B wipe. Caller must validate startup confirmation.
        Deletes trading tables, every user/credential/config, ML data/model,
        Excel history, and execution-candidate telemetry.
        """
        summary = {}
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()

                # ── 1. Hapus semua tabel di kara_data.db ────────────────
                all_tables = [
                    "paper_positions",
                    "paper_state",
                    "signals_history",
                    "risk_state",
                    "meta_pattern_stats",
                    "vol_cache",
                    "oi_snapshots",
                    "history_snapshots",
                    "trade_history",
                    "execution_candidates",
                    "bybit_positions",
                    "weak_confirmation_events",
                    "weak_confirmation_outcomes",
                ]
                for table in all_tables:
                    try:
                        cursor.execute(f"DELETE FROM {table}")
                        summary[table] = cursor.rowcount
                    except Exception as te:
                        # Tabel mungkin belum ada di DB lama — skip, bukan error fatal
                        log.debug(f"[RESET] Tabel {table} tidak ada atau gagal: {te}")
                        summary[table] = 0

                conn.commit()
                log.warning(f"🧹 [RESET] kara_data.db wiped: {summary}")

                # ── 2. Hapus kara_ml.db (experience buffer ML) ──────────
                ml_db_path = os.path.join(config.STORAGE_DIR, "kara_ml.db")
                if os.path.exists(ml_db_path):
                    os.remove(ml_db_path)
                    summary["kara_ml.db"] = "deleted"
                    log.warning(f"🧹 [RESET] kara_ml.db deleted")
                else:
                    summary["kara_ml.db"] = "not_found"

                # ── 3. Hapus kara_intelligence.pkl (trained ML model) ───
                pkl_path = os.path.join(config.STORAGE_DIR, "kara_intelligence.pkl")
                if os.path.exists(pkl_path):
                    os.remove(pkl_path)
                    summary["kara_intelligence.pkl"] = "deleted"
                    log.warning(f"🧹 [RESET] kara_intelligence.pkl deleted")
                else:
                    summary["kara_intelligence.pkl"] = "not_found"

                xlsx_path = os.path.join(config.STORAGE_DIR, "trade_history.xlsx")
                if os.path.exists(xlsx_path):
                    os.remove(xlsx_path)
                    summary["trade_history.xlsx"] = "deleted"
                else:
                    summary["trade_history.xlsx"] = "not_found"

                # ── 4. Delete every user, credential, and user config ───
                deleted_count = len(self.users)
                self.users = {}
                self.save()
                summary["users_deleted"] = deleted_count
                log.warning(
                    f"🧹 [RESET] users.json: {deleted_count} users deleted"
                )

                summary["status"] = "ok"
                return summary

            except Exception as e:
                log.error(f"❌ [RESET] Hard reset gagal: {e}")
                summary["status"] = "error"
                summary["error"] = str(e)
                return summary

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

    def save_execution_candidate(
        self,
        chat_id: str,
        sig: TradeSignal,
        *,
        status: str,
        reason: str,
        execution_environment: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist rejected candidate telemetry without inventing execution data."""
        if not status or not reason or not execution_environment:
            raise ValueError("Candidate status, reason, and environment are required")
        data = {
            "signal_id": sig.signal_id,
            "asset": sig.asset,
            "side": sig.side.value,
            "score": sig.score,
            "entry_price": sig.entry_price,
            "execution_environment": execution_environment,
            "status": status,
            "reason": reason,
            **(extra or {}),
        }
        candidate_id = f"{sig.signal_id}:{chat_id}:{status}"
        with self._lock:
            try:
                conn = self._get_conn()
                conn.execute(
                    """
                    INSERT OR REPLACE INTO execution_candidates
                    (candidate_id, chat_id, asset, side, status, reason,
                     execution_environment, data, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate_id, str(chat_id), sig.asset, sig.side.value,
                        status, reason, execution_environment,
                        json.dumps(data, default=str), sig.timestamp.timestamp(),
                    ),
                )
                conn.commit()
            except Exception as exc:
                log.error("Failed to save execution candidate %s: %s", candidate_id, exc)

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

    @staticmethod
    def meta_pattern_hierarchy(pattern_key: str) -> List[tuple[str, str]]:
        """Return specific and aggregate keys without guessing asset symbols."""
        if not pattern_key:
            return []
        buckets = ("s72p", "s65_71", "s60_64", "sub60")
        parts = pattern_key.split("_")
        if len(parts) >= 2 and "_".join(parts[-2:]) in buckets:
            bucket = "_".join(parts[-2:])
            base_parts = parts[:-2]
        elif parts and parts[-1] in buckets:
            bucket = parts[-1]
            base_parts = parts[:-1]
        else:
            bucket = ""
            base_parts = parts
        if len(base_parts) < 3:
            return [("specific", pattern_key)]
        mode, side = base_parts[0], base_parts[-1]
        asset_side = "_".join(base_parts)
        keys = [("specific", pattern_key), ("asset_side", asset_side)]
        if bucket:
            keys.append(("side_bucket", f"{mode}_{side}_{bucket}"))
        keys.append(("side", f"{mode}_{side}"))
        return keys

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
                for _, key in self.meta_pattern_hierarchy(pattern_key):
                    cursor.execute(
                        "SELECT winrate_ema, pnl_ema, samples FROM meta_pattern_stats WHERE pattern_key = ?",
                        (key,)
                    )
                    row = cursor.fetchone()
                    if row:
                        old_wr, old_pnl, old_n = float(row[0]), float(row[1]), int(row[2])
                        wr = (1 - alpha) * old_wr + alpha * win
                        pnl = (1 - alpha) * old_pnl + alpha * float(pnl_usd)
                        n = old_n + 1
                    else:
                        wr, pnl, n = win, float(pnl_usd), 1
                    cursor.execute(
                        "INSERT OR REPLACE INTO meta_pattern_stats (pattern_key, winrate_ema, pnl_ema, samples, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (key, wr, pnl, n, datetime.now(timezone.utc).timestamp())
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

    def save_bybit_position(
        self,
        chat_id: str,
        position: Position,
        symbol: str,
        live_status: str,
        entry_order_link_id: str = "",
    ):
        with self._lock:
            conn = self._get_conn()
            data = (
                position.model_dump_json()
                if hasattr(position, "model_dump_json")
                else json.dumps(position.dict(), default=str)
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO bybit_positions
                (position_id, chat_id, symbol, side, live_status,
                 entry_order_link_id, data, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position.position_id,
                    str(chat_id),
                    symbol,
                    position.side.value,
                    live_status,
                    entry_order_link_id,
                    data,
                    datetime.now(timezone.utc).timestamp(),
                ),
            )
            conn.commit()

    def load_bybit_positions(self, chat_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._get_conn().execute(
                """
                SELECT symbol, live_status, entry_order_link_id, data
                FROM bybit_positions WHERE chat_id = ?
                """,
                (str(chat_id),),
            ).fetchall()
        result = []
        for symbol, live_status, order_link_id, data in rows:
            result.append({
                "symbol": symbol,
                "live_status": live_status,
                "entry_order_link_id": order_link_id or "",
                "position": Position(**json.loads(data)),
            })
        return result

    def remove_bybit_position(self, position_id: str):
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "DELETE FROM bybit_positions WHERE position_id = ?",
                (position_id,),
            )
            conn.commit()

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

    def save_user(self):
        # ... existing save logic ... (was already here)
        pass

    # ── TRADE HISTORY (New Persistence) ───────────────────────────────

    def save_trade(self, chat_id: str, trade_data: Dict[str, Any]):
        """Persist a closed trade record to SQLite."""
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                trade_id = trade_data.get("pos_id", f"t_{int(datetime.now().timestamp())}")
                cursor.execute("""
                    INSERT OR REPLACE INTO trade_history (trade_id, chat_id, asset, side, pnl_usd, pnl_pct, data, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    trade_id,
                    str(chat_id),
                    trade_data.get("asset"),
                    trade_data.get("side"),
                    float(trade_data.get("pnl", 0)),
                    float(trade_data.get("pnl_pct", 0)),
                    json.dumps(trade_data, default=str),
                    datetime.now(timezone.utc).timestamp()
                ))
                conn.commit()
                log.debug(f"✓ Trade {trade_id} saved for user {chat_id}")
            except Exception as e:
                log.error(f"Failed to save trade to DB: {e}")

    def get_trade_history(self, chat_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Fetch historical trades for a specific user (includes created_at)."""
        trades = []
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT data, created_at, pnl_usd, pnl_pct, asset, side
                    FROM trade_history
                    WHERE chat_id = ?
                    ORDER BY created_at DESC LIMIT ?
                """, (str(chat_id), limit))
                rows = cursor.fetchall()
                for data_str, created_at, pnl_usd, pnl_pct, asset, side in rows:
                    try:
                        t = json.loads(data_str) if data_str else {}
                    except Exception:
                        t = {}
                    if not isinstance(t, dict):
                        t = {}
                    # Normalize fields for daily report / analytics
                    t.setdefault("type", "close")
                    t.setdefault("asset", asset)
                    t.setdefault("side", side)
                    if t.get("pnl") is None and pnl_usd is not None:
                        t["pnl"] = float(pnl_usd)
                    if t.get("pnl_pct") is None and pnl_pct is not None:
                        t["pnl_pct"] = float(pnl_pct)
                    t["created_at"] = created_at
                    # Keep both keys: many callers filter on timestamp
                    if "timestamp" not in t or t.get("timestamp") is None:
                        t["timestamp"] = created_at
                    trades.append(t)
                log.debug(f"get_trade_history: chat_id={chat_id!r} → {len(trades)} trades from DB at {self.db_path}")
            except Exception as e:
                log.error(f"Error fetching trade history for {chat_id}: {e}")
        return trades

    # Alias used by older callers (daily report, telegram)
    def load_trade_history(self, chat_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        return self.get_trade_history(chat_id, limit=limit)

    def get_all_trade_history(
        self,
        limit: int = 50_000,
        since_ts: float | None = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch closed trades across ALL users for weekly AI audit.
        Returns newest-first, optional since_ts (unix seconds, UTC).
        """
        trades: List[Dict[str, Any]] = []
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                if since_ts is not None:
                    cursor.execute(
                        """
                        SELECT data, created_at, pnl_usd, pnl_pct, asset, side, chat_id
                        FROM trade_history
                        WHERE created_at >= ?
                        ORDER BY created_at DESC
                        LIMIT ?
                        """,
                        (float(since_ts), int(limit)),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT data, created_at, pnl_usd, pnl_pct, asset, side, chat_id
                        FROM trade_history
                        ORDER BY created_at DESC
                        LIMIT ?
                        """,
                        (int(limit),),
                    )
                for data_str, created_at, pnl_usd, pnl_pct, asset, side, chat_id in cursor.fetchall():
                    try:
                        t = json.loads(data_str) if data_str else {}
                    except Exception:
                        t = {}
                    if not isinstance(t, dict):
                        t = {}
                    t.setdefault("type", "close")
                    t.setdefault("action", t.get("type", "close"))
                    t.setdefault("asset", asset)
                    t.setdefault("side", side)
                    t.setdefault("chat_id", chat_id)
                    if t.get("pnl") is None and pnl_usd is not None:
                        t["pnl"] = float(pnl_usd)
                    if t.get("pnl_pct") is None and pnl_pct is not None:
                        t["pnl_pct"] = float(pnl_pct)
                    t["created_at"] = created_at
                    if "timestamp" not in t or t.get("timestamp") is None:
                        t["timestamp"] = created_at
                    trades.append(t)
                log.info(
                    "get_all_trade_history: %d trades (limit=%s since=%s) from %s",
                    len(trades),
                    limit,
                    since_ts,
                    self.db_path,
                )
            except Exception as e:
                log.error(f"Error fetching all trade history: {e}")
        return trades

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
                        for field in ("hl_agent_secret", "bybit_api_key", "bybit_api_secret"):
                            secret = getattr(user_obj, field, None)
                            if secret and _fernet and secret.startswith("gAAAA"):
                                try:
                                    setattr(user_obj, field, _fernet.decrypt(secret.encode()).decode())
                                except Exception as e:
                                    log.error(f"Failed to decrypt {field} for {chat_id}: {e}")
                                    setattr(user_obj, field, None)
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
                        for field in ("hl_agent_secret", "bybit_api_key", "bybit_api_secret"):
                            secret = udict.get(field)
                            if not secret:
                                continue
                            if not _fernet:
                                if field.startswith("bybit_"):
                                    raise RuntimeError(
                                        "FERNET_KEY required before saving Bybit credentials"
                                    )
                                log.error("Saving legacy agent secret without encryption")
                                continue
                            if not secret.startswith("gAAAA"):
                                udict[field] = _fernet.encrypt(secret.encode()).decode()
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
                trading_mode="scalper",
                bot_mode=BotMode.PAPER,
                risk_pct=0.02
            )
        )
        self.users[str(chat_id)] = user
        self.save()
        return user

    def delete_user(self, chat_id: str) -> bool:
        """Remove user from JSON registry + paper positions/state for that chat."""
        cid = str(chat_id)
        removed = False
        if cid in self.users:
            del self.users[cid]
            self.save()
            removed = True
        try:
            self.clear_paper_positions(cid)
        except Exception as e:
            log.warning(f"clear_paper_positions({cid}) failed: {e}")
        try:
            with self._lock:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute("DELETE FROM paper_state WHERE chat_id = ?", (cid,))
                conn.commit()
        except Exception as e:
            log.warning(f"delete paper_state({cid}) failed: {e}")
        if removed:
            log.info(f"🗑️ Deleted user session {cid}")
        return removed

    def purge_dummy_users(
        self,
        dummy_ids: list[str] | None = None,
    ) -> list[str]:
        """
        Remove placeholder / template chat IDs that should never trade.
        Default: 123456789, 987654321 (from .env examples / old Meridian leftovers).
        """
        ids = [str(x) for x in (dummy_ids or ["123456789", "987654321"])]
        purged: list[str] = []
        for cid in ids:
            had_user = cid in self.users
            self.delete_user(cid)
            try:
                self.clear_paper_positions(cid)
            except Exception:
                pass
            if had_user:
                purged.append(cid)
            else:
                # Still report so callers strip Telegram auth state
                purged.append(cid)
        log.info(f"🧹 Purged dummy chat_ids: {', '.join(purged)}")
        return purged

# Global instance
user_db = UserDB()

