"""
Hard Reset Script — KARA v7.x
Cara pakai:
  cd "d:/Vibe Coding/KARA - 400"
  python scratch/hard_reset_users.py

Yang dihapus:
  - Semua posisi, balance, trade history, meta pattern, OI snapshots
  - kara_ml.db (experience buffer ML)
  - kara_intelligence.pkl (trained model)
  - Balance user di users.json direset ke default

Yang TIDAK dihapus:
  - Config user (leverage, risk preference, wallet address)
  - Akses Telegram (chat_id tetap terdaftar)
"""

import sys
import os

# Pastikan import dari root project
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger("kara.reset")

def main():
    confirm = input("⚠️  Ini akan menghapus SEMUA data trading. Ketik 'RESET' untuk lanjutkan: ").strip()
    if confirm != "RESET":
        log.info("Dibatalkan.")
        return

    from core.db import user_db
    summary = user_db.hard_reset_all_data()

    log.info("✅ Hard reset selesai. Ringkasan:")
    for k, v in summary.items():
        log.info(f"   {k}: {v}")

if __name__ == "__main__":
    main()
