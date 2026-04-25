import logging

log = logging.getLogger("DynamicRisk")

def calculate_risk_multiplier(expected_edge: float) -> float:
    """
    Given an ML predicted win probability (0.0 to 1.0),
    compute a multiplier to dynamically scale the risk position size.
    0.5 is neutral (1x). Range capped at 0.85x-1.15x while model data is limited (<500 trades).
    Expand range to 0.6x-1.4x once model has 500+ validated out-of-sample trades.
    """
    if expected_edge is None:
        return 1.0

    if expected_edge < 0.4:
        return 0.85  # Soft reduction — pre_trade_check will abort if ENABLE_INTELLIGENCE=True.

    # Conservative range: ±15% max while model is still learning.
    # Edge 0.8 -> 1.0 + 0.3*0.3 = 1.09x
    # Edge 0.9 -> 1.0 + 0.4*0.3 = 1.12x
    # Edge 0.6 -> 1.0 + 0.1*0.3 = 1.03x
    multiplier = 1.0 + ((expected_edge - 0.5) * 0.3)

    multiplier = max(0.85, min(1.15, multiplier))
    return multiplier
