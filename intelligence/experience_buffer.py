import sqlite3
import json
import logging
import os
import threading
from datetime import datetime, timezone
import config

log = logging.getLogger("ExperienceBuffer")

class ExperienceBuffer:
    def __init__(self):
        self.db_path = os.path.join(config.STORAGE_DIR, "kara_ml.db")
        self._lock = threading.Lock()
        self._init_db()

    def _get_conn(self):
        # sqlite3.connect membuat file baru jika tidak ada, tapi TIDAK membuat tabel.
        # Setelah hard reset menghapus kara_ml.db, file baru terbentuk tapi tabel kosong.
        # Solusi: cek keberadaan tabel setiap kali connect, buat jika belum ada.
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            conn.execute("SELECT 1 FROM ml_experience LIMIT 1")
        except sqlite3.OperationalError:
            # Tabel belum ada — ini terjadi setelah hard reset atau fresh deploy
            self._create_tables(conn)
        return conn

    def _create_tables(self, conn):
        """Buat schema tabel. Dipanggil saat file baru atau tabel tidak ada."""
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ml_experience (
                pos_id TEXT PRIMARY KEY,
                chat_id TEXT,
                timestamp REAL,
                asset TEXT,
                side TEXT,
                score INTEGER,
                meta_delta INTEGER,
                oi_score INTEGER,
                funding_score INTEGER,
                liq_score INTEGER,
                ob_score INTEGER,
                session_bonus INTEGER,
                funding_rate REAL,
                realized_vol REAL,
                trend_pct REAL,
                expected_edge REAL,
                actual_pnl_pct REAL,
                duration_sec REAL,
                is_win INTEGER
            )
        ''')
        # Preserve historical rows; future rows carry entry/exit state needed to
        # learn 1m crypto-perp follow-through instead of only final win/loss.
        existing = {row[1] for row in cursor.execute("PRAGMA table_info(ml_experience)")}
        for name, sql_type in (
            ("trade_mode", "TEXT"),
            ("entry_location_quality", "TEXT"),
            ("micro_risk_pct", "REAL"),
            ("exit_reason", "TEXT"),
            ("mfe_pct", "REAL"),
            ("time_exit_trigger", "TEXT"),
            ("impulse_win", "INTEGER"),
        ):
            if name not in existing:
                cursor.execute(f"ALTER TABLE ml_experience ADD COLUMN {name} {sql_type}")
        conn.commit()

    def _init_db(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._create_tables(conn)
            conn.close()

    def record_entry(self, chat_id: str, pos_id: str, asset: str, side: str, score: int, meta_delta: int, 
                     bd, funding_rate: float, realized_vol: float, trend_pct: float, expected_edge: float,
                     trade_mode: str = "standard", entry_location_quality: str = "unknown",
                     micro_risk_pct: float = 0.0):
        """Record the point-in-time features of a new position."""
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                
                # Handle bd safely
                oi_score = bd.oi_funding_score if bd else 0
                liq_score = bd.liquidation_score if bd else 0
                ob_score = bd.orderbook_score if bd else 0
                session_bonus = bd.session_bonus if bd else 0
                
                cursor.execute('''
                    INSERT OR IGNORE INTO ml_experience (
                        pos_id, chat_id, timestamp, asset, side, score, meta_delta,
                        oi_score, funding_score, liq_score, ob_score, session_bonus,
                        funding_rate, realized_vol, trend_pct, expected_edge,
                        actual_pnl_pct, duration_sec, is_win, trade_mode,
                        entry_location_quality, micro_risk_pct, exit_reason, mfe_pct,
                        time_exit_trigger, impulse_win
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, NULL, NULL, NULL, NULL)
                ''', (
                    pos_id, str(chat_id), datetime.now(timezone.utc).timestamp(), asset, side, score, meta_delta,
                    oi_score, 0, liq_score, ob_score, session_bonus,  # funding_score disimpan dari funding_rate, bukan oi duplikat
                    funding_rate, realized_vol, trend_pct, expected_edge,
                    trade_mode, entry_location_quality, float(micro_risk_pct or 0.0)
                ))
                conn.commit()
                conn.close()
                log.debug(f"🧠 [Buffer] Recorded entry features for {pos_id} (User: {chat_id})")
            except Exception as e:
                log.error(f"Failed to record ML entry {pos_id}: {e}")

    def update_label(
        self, pos_id: str, pnl_pct: float, duration_sec: float,
        exit_reason: str = "", mfe_pct: float = 0.0, time_exit_trigger: str = ""
    ):
        """Update final outcome plus scalper follow-through label on full close."""
        is_win = 1 if pnl_pct > 0 else 0
        impulse_win = 1 if pnl_pct > 0 and mfe_pct >= 0.0035 else 0
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE ml_experience 
                    SET actual_pnl_pct = ?, duration_sec = ?, is_win = ?, exit_reason = ?,
                        mfe_pct = ?, time_exit_trigger = ?, impulse_win = ?
                    WHERE pos_id = ?
                ''', (
                    float(pnl_pct), float(duration_sec), is_win, exit_reason,
                    float(mfe_pct or 0.0), time_exit_trigger, impulse_win, pos_id
                ))
                conn.commit()
                conn.close()
                log.debug(f"🧠 [Buffer] Updated label for {pos_id} (Win: {is_win})")
            except Exception as e:
                log.error(f"Failed to update ML label {pos_id}: {e}")

    def get_training_data(self, enriched_only: bool = False):
        """Fetch all closed positions for training"""
        with self._lock:
            try:
                conn = self._get_conn()
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                query = "SELECT * FROM ml_experience WHERE is_win IS NOT NULL"
                if enriched_only:
                    query += " AND trade_mode IS NOT NULL AND exit_reason IS NOT NULL"
                cursor.execute(query)
                rows = cursor.fetchall()
                conn.close()
                return [dict(row) for row in rows]
            except Exception as e:
                log.error(f"Failed to fetch training data: {e}")
                return []

experience_buffer = ExperienceBuffer()
