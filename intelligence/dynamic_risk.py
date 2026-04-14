import logging

log = logging.getLogger("DynamicRisk")

def calculate_risk_multiplier(expected_edge: float) -> float:
    """
    Given an ML predicted win probability (0.0 to 1.0),
    compute a multiplier to dynamically scale the risk position size.
    0.5 is neutral (1x).
    < 0.4 -> 0x (or 0.5x, we will abort strictly in risk manager).
    > 0.8 -> aggressive boost.
    """
    if expected_edge is None:
        return 1.0

    if expected_edge < 0.4:
        return 0.5  # Soft fallback, but pre_trade_check will abort it.
        
    # Formula Example: 1.0 + (edge - 0.5) * 2
    # Edge 0.8 -> 1.0 + 0.3*2 = 1.6x risk.
    # Edge 0.9 -> 1.0 + 0.4*2 = 1.8x risk.
    # Edge 0.6 -> 1.0 + 0.1*2 = 1.2x risk.
    multiplier = 1.0 + ((expected_edge - 0.5) * 2.0)
    
    # Cap multiplier to bounds
    multiplier = max(0.5, min(2.5, multiplier))
    return multiplier
