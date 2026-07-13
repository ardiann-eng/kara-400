import logging

log = logging.getLogger("FeatureEngine")

def extract_live_features(score: int, meta_delta: int, bd, funding_rate: float, realized_vol: float, trend_pct: float,
                          micro_risk_pct: float = 0.0, entry_location_quality: str = "unknown",
                          trade_mode: str = "standard") -> list:
    """
    Extract perfectly aligned feature array for the Intelligence Model.
    Must match IntelligenceModel.get_features(). Exit outcomes never enter this array.
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
            float(trend_pct),
            float(micro_risk_pct),
            {"invalid": 0.0, "weak": 1.0, "weak_confirmed": 1.0, "valid": 2.0, "excellent": 3.0}.get(entry_location_quality, -1.0),
            1.0 if trade_mode == "scalper" else 0.0,
        ]
    except Exception as e:
        log.error(f"Feature extraction failed: {e}")
        return [0.0] * 12
