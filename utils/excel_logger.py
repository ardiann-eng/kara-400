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
            "PnL (%)", "Score", "Reason", "Mode", "Position ID"
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

    def log_trade(self, chat_id: str, data: Dict[str, Any]):
        """Append a new trade action to the Excel file."""
        try:
            # Prepare row data
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
                "Position ID": data.get("pos_id", "")
            }

            # Read, append, write
            if os.path.exists(self.file_path):
                # Ensure we specify that header is on row 0
                df = pd.read_excel(self.file_path, engine='openpyxl', header=0)
            else:
                df = pd.DataFrame(columns=self._columns)
            
            new_row_df = pd.DataFrame([row])
            
            # Use concat but only if new_row_df has data
            if not new_row_df.empty:
                df = pd.concat([df, new_row_df], ignore_index=True)
                df.to_excel(self.file_path, index=False, engine='openpyxl')
                log.debug(f" Logged trade to Excel for {chat_id}: {row['Asset']} {row['Action']}")
            
        except Exception as e:
            log.error(f" Failed to log trade to Excel: {e}")

    def clear_trades_for_user(self, chat_id: str) -> int:
        """Hapus semua baris trade milik chat_id dari Excel. Returns jumlah baris dihapus."""
        try:
            if not os.path.exists(self.file_path):
                return 0
            df = pd.read_excel(self.file_path, engine='openpyxl', header=0)
            mask = df["Chat ID"].astype(str) == str(chat_id)
            count = int(mask.sum())
            if count > 0:
                df = df[~mask]
                df.to_excel(self.file_path, index=False, engine='openpyxl')
                log.info(f"clear_trades_for_user: removed {count} rows for chat_id={chat_id}")
            return count
        except Exception as e:
            log.error(f"Failed to clear Excel trades for {chat_id}: {e}")
            return 0


# Singleton instance
_logger = None

def get_excel_logger() -> TradeExcelLogger:
    global _logger
    if _logger is None:
        _logger = TradeExcelLogger()
    return _logger
