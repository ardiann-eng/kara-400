import os
import sys
import logging
from datetime import datetime, timezone
import pandas as pd
import sqlite3

# Add parent directory to path to allow imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.experience_buffer import experience_buffer
from intelligence.intelligence_model import intelligence_model

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("HistoryIngestor")

def ingest_excel_history(excel_path="trade_history.xlsx"):
    """
    Reads the user's actual trade history from Excel and imports it into
    the Machine Learning Experience Buffer (ml_experience.db).
    Handles duplicates from multiple data sources.
    """
    if not os.path.exists(excel_path):
        log.error(f"❌ File {excel_path} not found in the root directory!")
        return

    log.info(f"📂 Loading data from {excel_path}...")
    try:
        df = pd.read_excel(excel_path)
        initial_len = len(df)
        # Deduplicate: Same Position ID + Action is redundant across multiple exports
        df = df.drop_duplicates(subset=['Position ID', 'Action'], keep='first')
        after_len = len(df)
        if initial_len > after_len:
            log.info(f"🧹 Removed {initial_len - after_len} duplicate rows.")
    except Exception as e:
        log.error(f"❌ Failed to read Excel file: {e}")
        return

    # Validasi Kolom
    required_cols = ['Position ID', 'Action', 'Score', 'PnL (%)', 'Side', 'Asset', 'Timestamp']
    for col in required_cols:
        if col not in df.columns:
            log.error(f"❌ Missing required column in Excel: {col}")
            return

    # Filter out trades without Position ID
    df = df.dropna(subset=['Position ID'])

    # Kelompokkan data per Position ID untuk memisahkan baris OPEN dan CLOSE
    trades_ingested = 0
    conn = experience_buffer._get_conn()
    cursor = conn.cursor()

    grouped = df.groupby('Position ID')
    for pos_id, group in grouped:
        try:
            # Cari baris entry (OPEN) dan exit (CLOSE)
            open_rows = group[group['Action'].str.upper() == 'OPEN']
            close_rows = group[group['Action'].str.upper() == 'CLOSE']
            
            if open_rows.empty or close_rows.empty:
                continue # Trade belum selesai atau data cacat, skip.
                
            open_row = open_rows.iloc[0]
            # Ambil PnL final dari penutupan (bisa jadi ada multiple close, kita ambil yang terakhir)
            close_row = close_rows.iloc[-1] 

            score = float(open_row['Score']) if pd.notna(open_row['Score']) else 50.0
            pnl_pct = float(close_row['PnL (%)']) if pd.notna(close_row['PnL (%)']) else None
            
            if pnl_pct is None:
                continue
                
            is_win = 1 if pnl_pct > 0 else 0
            
            # Waktu
            try:
                open_time = pd.to_datetime(open_row['Timestamp'])
                close_time = pd.to_datetime(close_row['Timestamp'])
                duration_sec = (close_time - open_time).total_seconds()
            except:
                duration_sec = 1800 # Default fallback
            
            # Feature extraction baseline
            meta_delta = 0 # Default if not in Excel
            # Using 1/5th score as a rough proxy for individual analyzer contributions if unknown
            # This helps avoid 'all zero' inputs for historical trades where we only have the total score.
            oi_score = score * 0.25
            liq_score = score * 0.25
            ob_score = score * 0.25
            session_bonus = 0
            # Rough estimation of funding based on side to provide some feature variance
            funding_rate = 0.0001 if open_row['Side'].upper() == 'LONG' else -0.0001
            vol = 0.02

            cursor.execute('''
                INSERT OR REPLACE INTO ml_experience (
                    pos_id, timestamp, asset, side, score, meta_delta,
                    oi_score, funding_score, liq_score, ob_score, session_bonus,
                    funding_rate, realized_vol, trend_pct, expected_edge,
                    actual_pnl_pct, duration_sec, is_win
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                str(pos_id), open_time.timestamp(), str(open_row['Asset']), str(open_row['Side']), score, meta_delta,
                oi_score, oi_score, liq_score, ob_score, session_bonus,
                funding_rate, vol, 0.0, 0.5,
                pnl_pct, duration_sec, is_win
            ))
            
            trades_ingested += 1
            
        except Exception as e:
            log.debug(f"Skipping trade {pos_id} due to error: {e}")
            continue

    conn.commit()
    conn.close()
    
    log.info(f"✅ Berhasil memproses {trades_ingested} trade unik dari 7 sumber data.")



def test_online_feature_learner():
    log.info("🧠 Memulai pelatihan ulang Model AI dengan data Excel Trade History nyata...")
    intelligence_model.retrain()
    
    if intelligence_model.model is None:
        log.warning("⚠️ Model gagal dilatih, periksa apakah ada setidaknya 50 trade di database.")
        return

    # Uji Coba Simulasi
    # Feature format: [score, meta_delta, oi_score, liq_score, ob_score, session_bonus, funding_rate, realized_vol, trend_pct]
    good_features = [85.0, 5.0, 20.0, 15.0, 10.0, 14.0, 0.0002, 0.015, 0.002]
    bad_features = [45.0, -4.0, 5.0, 0.0, 0.0, -15.0, -0.0001, 0.05, -0.001]
    
    good_pred = intelligence_model.predict_edge(good_features)
    bad_pred = intelligence_model.predict_edge(bad_features)
    
    log.info(f"🔮 Test Prediksi (High Quality Score): {good_pred*100:.1f}% Probability Win")
    log.info(f"🔮 Test Prediksi (Low Quality Score): {bad_pred*100:.1f}% Probability Win")
    
if __name__ == "__main__":
    ingest_excel_history()
    test_online_feature_learner()
