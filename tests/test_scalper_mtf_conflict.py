from types import SimpleNamespace
import unittest
from unittest.mock import patch

import config
from engine.scoring_engine import ScoringEngine
from models.schemas import Side


ASSET = "TEST"


def make_candles(side: Side) -> list[dict]:
    direction = 1 if side == Side.LONG else -1
    closes = [100.0 + direction * index * 0.08 for index in range(26)]
    closes.extend(closes[-1] + direction * move for move in (0.30, 0.65, 1.05, 1.50))
    candles = []
    for index, close in enumerate(closes):
        open_price = close - direction * 0.04
        volume = 1000.0 if index >= len(closes) - 2 else 50.0
        candles.append({
            "o": str(open_price),
            "h": str(max(open_price, close) + 0.03),
            "l": str(min(open_price, close) - 0.03),
            "c": str(close),
            "v": str(volume),
        })
    return candles


def make_engine(side: Side) -> ScoringEngine:
    bid_size, ask_size = (10.0, 1.0) if side == Side.LONG else (1.0, 10.0)
    trade_side = "B" if side == Side.LONG else "S"
    engine = ScoringEngine.__new__(ScoringEngine)
    engine.cache = SimpleNamespace(
        orderbook={
            ASSET: {
                "levels": [
                    [{"px": "99.99", "sz": str(bid_size)}],
                    [{"px": "100.01", "sz": str(ask_size)}],
                ]
            }
        },
        trades={ASSET: [{"side": trade_side, "sz": "1"} for _ in range(40)]},
    )
    return engine


def score(side: Side, mtf_trend: str):
    candles = make_candles(side)
    # Raise existing structure contribution so both LONG and SHORT fixtures are
    # indisputably high-score setups. The test then proves score cannot override
    # a directional MTF conflict; production scoring logic still runs unchanged.
    with patch.object(config.SIGNAL, "structure_scalper_bonus", 30):
        return make_engine(side)._calculate_scalper_score(
            ASSET,
            float(candles[-1]["c"]),
            candles,
            mtf_trend,
        )


class ScalperMtfConflictTests(unittest.TestCase):
    def test_high_score_long_rejected_when_15m_mtf_is_bear(self):
        neutral_score, neutral_side, _ = score(Side.LONG, "neutral")
        conflict_score, conflict_side, reasons = score(Side.LONG, "bear")

        self.assertEqual(neutral_side, Side.LONG)
        self.assertGreaterEqual(neutral_score, config.SCALPER.mtf_bonus_high_score)
        self.assertEqual(conflict_side, Side.LONG)
        self.assertEqual(conflict_score, 0)
        self.assertIn("REJECT: LONG conflicts with 15m MTF bear", reasons)

    def test_high_score_short_rejected_when_15m_mtf_is_bull(self):
        neutral_score, neutral_side, _ = score(Side.SHORT, "neutral")
        conflict_score, conflict_side, reasons = score(Side.SHORT, "bull")

        self.assertEqual(neutral_side, Side.SHORT)
        self.assertGreaterEqual(neutral_score, config.SCALPER.mtf_bonus_high_score)
        self.assertEqual(conflict_side, Side.SHORT)
        self.assertEqual(conflict_score, 0)
        self.assertIn("REJECT: SHORT conflicts with 15m MTF bull", reasons)

    def test_aligned_15m_mtf_retains_existing_bonus(self):
        neutral_score, neutral_side, _ = score(Side.LONG, "neutral")
        aligned_score, aligned_side, reasons = score(Side.LONG, "bull")

        expected_bonus = min(config.SCALPER.mtf_high_bonus, config.SCALPER.mtf_score_bonus)
        self.assertEqual(aligned_side, neutral_side)
        self.assertEqual(aligned_side, Side.LONG)
        self.assertGreaterEqual(aligned_score, neutral_score)
        self.assertIn(
            f"15m MTF Align (bull) -> +{expected_bonus} context bonus",
            " ".join(reasons),
        )

    def test_neutral_15m_mtf_retains_existing_score(self):
        neutral_score, neutral_side, reasons = score(Side.SHORT, "neutral")

        self.assertEqual(neutral_side, Side.SHORT)
        self.assertGreaterEqual(neutral_score, config.SCALPER.mtf_bonus_high_score)
        self.assertTrue(all("15m MTF" not in reason for reason in reasons))


if __name__ == "__main__":
    unittest.main()
