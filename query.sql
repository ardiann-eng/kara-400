SELECT pos_id, asset, side, score, oi_score, liq_score, ob_score, session_bonus, meta_delta, funding_rate, realized_vol, actual_pnl_pct, is_win, duration_sec, timestamp
FROM ml_experience 
WHERE actual_pnl_pct IS NOT NULL
ORDER BY timestamp