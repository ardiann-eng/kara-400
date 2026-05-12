"""
KARA Bot - Excel Trade Logger
Utility to maintain a persistent Excel file of all trade activities.
"""

import os
import pandas as pd
from datetime import datetime
import logging
from typing import Dict, Any

import config

log = logging.getLogger("kara.excel_logger")

class TradeExcelLogger:
    """Handles persistent logging of trades to an Excel file."""

    def __init__(self, file_path: str = None):
        self.file_path = file_path or config.EXCEL_LOG_PATH
        self._columns = [
            "Timestamp", "Chat ID", "Asset", "Side", "Action", 
            "Price", "Size", "Notional (USD)", "PnL ($)", 
            "PnL (%)", "Score", "Reason", "Mode", "Position ID",
            "Autopsy"
        ]
        self._ensure_file_exists()

    def _ensure_file_exists(self):
        """Create the file with headers if it doesn't exist."""
        if not os.path.exists(self.file_path):
            try:
                df = pd.DataFrame(columns=self._columns)
                df.to_excel(self.file_path, index=False, engine='openpyxl')
                log.info(f" Created new Excel log file: {self.file_path}")
            except Exception as e:
                log.error(f" Failed to create Excel log: {e}")

    def _recreate_file(self):
        """Recreate Excel file from scratch (called when file is corrupt)."""
        try:
            if os.path.exists(self.file_path):
                os.remove(self.file_path)
            df = pd.DataFrame(columns=self._columns)
            df.to_excel(self.file_path, index=False, engine='openpyxl')
            log.info(f"Excel log recreated (was corrupt): {self.file_path}")
        except Exception as e:
            log.warning(f"Excel recreate failed: {e}")

    def log_trade(self, chat_id: str, data: Dict[str, Any]):
        """Append a new trade action to the Excel file."""
        try:
            row = {
                "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "Chat ID": str(chat_id),
                "Asset": data.get("asset", "Unknown"),
                "Side": data.get("side", "").upper(),
                "Action": data.get("type", "unknown").upper(),
                "Price": data.get("price", data.get("entry_price", data.get("exit_price", 0))),
                "Size": data.get("size", data.get("contracts", 0)),
                "Notional (USD)": data.get("notional", 0),
                "PnL ($)": data.get("pnl", 0),
                "PnL (%)": data.get("pnl_pct", 0),
                "Score": data.get("score", 0),
                "Reason": data.get("reason", ""),
                "Mode": config.TRADE_MODE.upper(),
                "Position ID": data.get("pos_id", ""),
                "Autopsy": data.get("autopsy", ""),
            }

            try:
                if os.path.exists(self.file_path):
                    df = pd.read_excel(self.file_path, engine='openpyxl', header=0)
                else:
                    df = pd.DataFrame(columns=self._columns)
            except Exception:
                # File corrupt — recreate silently
                self._recreate_file()
                df = pd.DataFrame(columns=self._columns)

            new_row_df = pd.DataFrame([row])
            if not new_row_df.empty:
                df = pd.concat([df, new_row_df], ignore_index=True)
                df.to_excel(self.file_path, index=False, engine='openpyxl')
                if row["Action"] == "CLOSE":
                    self._log_top_insights(chat_id)

        except Exception as e:
            log.debug(f"Excel log skipped: {e}")

    def _log_top_insights(self, chat_id: str):
        """Analyze last 20 trades and log the top actionable insight."""
        try:
            if not os.path.exists(self.file_path):
                return
            
            df = pd.read_excel(self.file_path, engine='openpyxl', header=0)
            user_trades = df[df["Chat ID"].astype(str) == str(chat_id)].tail(20)
            
            if len(user_trades) < 5: # Need a minimum sample
                return
                
            from memory.autopsy_engine import autopsy_engine
            # Convert DF rows to objects or dicts for the engine
            trades_list = user_trades.to_dict('records')
            insight = autopsy_engine.get_top_insight(trades_list)
            
            log.info(f"🔥 [INSIGHT] {chat_id} | {insight}")
        except Exception as e:
            log.debug(f"Failed to generate top insight: {e}")

    def clear_trades_for_user(self, chat_id: str) -> int:
        """Hapus semua baris trade milik chat_id dari Excel. Returns jumlah baris dihapus."""
        try:
            if not os.path.exists(self.file_path):
                return 0
            try:
                df = pd.read_excel(self.file_path, engine='openpyxl', header=0)
            except Exception:
                self._recreate_file()
                return 0
            mask = df["Chat ID"].astype(str) == str(chat_id)
            count = int(mask.sum())
            if count > 0:
                df = df[~mask]
                df.to_excel(self.file_path, index=False, engine='openpyxl')
                log.info(f"clear_trades_for_user: removed {count} rows for chat_id={chat_id}")
            return count
        except Exception as e:
            log.debug(f"Failed to clear Excel trades for {chat_id}: {e}")
            return 0


# Singleton instance
_logger = None

def get_excel_logger() -> TradeExcelLogger:
    global _logger
    if _logger is None:
        _logger = TradeExcelLogger()
    return _logger
