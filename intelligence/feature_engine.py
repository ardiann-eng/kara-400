import logging

log = logging.getLogger("FeatureEngine")

def extract_live_features(score: int, meta_delta: int, bd, funding_rate: float, realized_vol: float, trend_pct: float) -> list:
    """
    Extract perfectly aligned feature array for the Intelligence Model.
    Must match the exact order of IntelligenceModel.get_features():
    [score, meta_delta, oi_score, liq_score, ob_score, session_bonus, funding_rate, realized_vol, trend_pct]
    """
    try:
        oi_score = float(getattr(bd, "oi_funding_score", 0)) if bd else 0.0
        liq_score = float(getattr(bd, "liquidation_score", 0)) if bd else 0.0
        ob_score = float(getattr(bd, "orderbook_score", 0)) if bd else 0.0
        session_bonus = float(getattr(bd, "session_bonus", 0)) if bd else 0.0
        
        return [
            float(score),
            float(meta_delta),
            oi_score,
            liq_score,
            ob_score,
            session_bonus,
            float(funding_rate),
            float(realized_vol),
            float(trend_pct)
        ]
    except Exception as e:
        log.error(f"Feature extraction failed: {e}")
        return [0.0] * 9
