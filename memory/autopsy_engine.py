"""
KARA Bot — Rule-Based Post-Mortem Autopsy Engine.

Generates deterministic autopsy reports for every closed trade.
Zero API calls, zero cost, zero latency.

Each rule is a (condition, template, priority) tuple.
First matching rule wins (sorted by priority ascending).
"""

import logging
from collections import Counter
from typing import Dict, List, Optional

log = logging.getLogger("kara.autopsy")


class AutopsyEngine:
    """Rule-based trade autopsy generator."""

    def __init__(self):
        self.rules = []
        self._build_rules()

    def _build_rules(self):
        """Build priority-sorted rule list. Lower priority = checked first."""

        self.rules = [
            # === CRITICAL PATTERNS (Priority 1-20) ===

            (
                lambda d: d["exit_reason"] == "time_exit" and d["pnl"] > 0 and d["time_held_min"] >= 18,
                "⏰ TIME-EXIT CUT WINNER: Profit ${pnl:.2f} dipotong paksa di menit ke-{time_held_min:.0f}. "
                "Winner ini seharusnya hold lebih lama (score={score}). "
                "Saran: naikkan time_exit untuk score {score} atau aktifkan trailing stop lebih awal.",
                1
            ),
            (
                lambda d: d["exit_reason"] == "time_exit" and d["pnl"] < -1.0 and d["time_held_min"] >= 18,
                "⏰ TIME-EXIT CUT LOSS: Rugi ${pnl:.2f} di menit ke-{time_held_min:.0f}. "
                "Entry timing buruk — harga berlawanan langsung setelah entry. "
                "Saran: perketat entry filter untuk {asset} atau turunkan time_exit jika score < 60.",
                2
            ),
            (
                lambda d: d["exit_reason"] == "stop_loss" and d.get("trend_pct", 0) > 0.03 and d["side"] == "LONG",
                "🛑 SL KENA SAAT LATE TREND: Entry chase trend {trend_pct:.1%}/1h. "
                "ATR SL ({sl_distance_pct:.2%}) terlalu sempit untuk momentum chase, atau entry terlambat. "
                "Saran: kalau trend >3%, turunkan size 50% atau jangan entry.",
                3
            ),
            (
                lambda d: d["exit_reason"] == "stop_loss" and d.get("atr_pct", 0) > 0 and abs(d["pnl"]) > d["atr_pct"] * 2 * (d.get("notional", 100)),
                "🛑 SL HEMORRHAGE: Loss ${pnl:.2f} lebih besar dari 2× ATR ({atr_pct:.2%}). "
                "SL tidak adaptif atau slippage parah di {asset}. "
                "Saran: periksa slippage protection atau turunkan leverage.",
                4
            ),
            (
                lambda d: d.get("funding_rate", 0) > 0.0003 and d["side"] == "LONG" and d["pnl"] < 0,
                "📉 FUNDING FADE GAGAL: Entry LONG saat funding positif ekstrem ({funding_rate:.4%}). "
                "Crowded longs tapi harga tetap turun. "
                "Saran: funding contrarian memang risky — perketat SL 0.8× atau skip {asset} kalau funding >0.03%.",
                5
            ),
            (
                lambda d: d.get("funding_rate", 0) < -0.0003 and d["side"] == "SHORT" and d["pnl"] < 0,
                "📉 FUNDING FADE GAGAL: Entry SHORT saat funding negatif ekstrem ({funding_rate:.4%}). "
                "Crowded shorts squeeze. "
                "Saran: perketat SL atau skip contrarian SHORT saat funding < -0.03%.",
                5
            ),
            (
                lambda d: d["score"] >= 65 and d["pnl"] < -0.5,
                "🎯 HIGH-SCORE TOXIC: Score {score} counter-predictive (loss ${pnl:.2f}). "
                "Mean-reversion guard atau regime multiplier masih salah. "
                "Saran: periksa apakah score 65+ sering entry di puncak momentum.",
                6
            ),
            (
                lambda d: d["score"] <= 55 and d["pnl"] > 0.5,
                "🎯 LOW-SCORE GOLD: Score {score} justru cuan ${pnl:.2f}. "
                "Bot terlalu skeptis pada sinyal lemah — pertimbangkan turunkan min_score.",
                7
            ),

            # === VOLATILITY & ASSET PATTERNS (Priority 21-40) ===

            (
                lambda d: d.get("realized_vol", 0) > 0.06 and d["pnl"] < -1.0,
                "🌊 HIGH-VOL WIPEOUT: Vol {realized_vol:.1%} terlalu gila untuk scalper. "
                "{asset} menghancurkan RR. "
                "Saran: blacklist {asset} kalau vol >6% atau size turun 75%.",
                21
            ),
            (
                lambda d: d.get("max_drawdown", 0) < 0 and abs(d.get("max_drawdown", 0)) > abs(d["pnl"]) * 3 and d["pnl"] > 0,
                "🌊 DEEP DRAWDOWN RECOVERY: Max DD ${max_drawdown:.2f} tapi close profit ${pnl:.2f}. "
                "Terlalu beresiko — scalper tidak boleh floating loss 3× lebih dalam dari profit akhir. "
                "Saran: perketat SL atau turunkan leverage.",
                22
            ),
            (
                lambda d: d.get("sl_distance_pct", 0) > 0.025 and d["pnl"] < 0 and d["exit_reason"] != "stop_loss",
                "🛡️ SL TERLALU LEBAR: SL {sl_distance_pct:.2%} tapi exit via {exit_reason}, bukan SL. "
                "Loss lebih kecil dari SL, tapi SL yang lebar membuat bot tidak berani entry lain. "
                "Saran: ATR SL ceiling turun ke 2.0%.",
                23
            ),

            # === EXIT REASON PATTERNS (Priority 41-60) ===

            (
                lambda d: d["exit_reason"] == "trailing_stop" and d["pnl"] > 0,
                "🏃 TRAILING STOP WINNER: Profit ${pnl:.2f} via trailing. "
                "Ini exit terbaik bot — pertimbangkan naikkan trailing frequency atau turunkan activation threshold.",
                41
            ),
            (
                lambda d: d["exit_reason"] == "trailing_stop" and d["pnl"] < 0,
                "🏃 TRAILING STOP LOSS: Trailing stop kena loss ${pnl:.2f}. "
                "Trailing terlalu ketat atau volatility spike. "
                "Saran: naikkan trailing step.",
                42
            ),
            (
                lambda d: d["exit_reason"] in ("tp1", "tp2") and d["time_held_min"] < 3,
                "⚡ TP FLASH: Profit ${pnl:.2f} dalam {time_held_min:.0f} menit via {exit_reason}. "
                "Scalp cepat — TP mungkin terlalu dekat entry. "
                "Saran: kalau sering flash TP, naikkan TP ke 1.2× SL.",
                43
            ),
            (
                lambda d: d["exit_reason"] == "momentum_exit" and d["pnl"] > 0,
                "📊 MOMENTUM EXIT WIN: Profit ${pnl:.2f} saat indikator teknikal degradasi. "
                "Exit tepat waktu sebelum reversal penuh.",
                44
            ),
            (
                lambda d: d["exit_reason"] == "momentum_exit" and d["pnl"] < 0,
                "📊 MOMENTUM EXIT LOSS: Loss ${pnl:.2f} via momentum degradation. "
                "Indikator sudah berlawanan saat entry — timing buruk. "
                "Saran: tambah filter momentum di entry scoring.",
                45
            ),

            # === DEFAULT / FALLBACK (Priority 100) ===

            (
                lambda d: d["pnl"] > 0,
                "✅ WIN: ${pnl:.2f} via {exit_reason}. Score {score}, hold {time_held_min:.0f}m. "
                "Pola ini valid — pertahankan.",
                100
            ),
            (
                lambda d: d["pnl"] <= 0,
                "❌ LOSS: ${pnl:.2f} via {exit_reason}. Score {score}, hold {time_held_min:.0f}m. "
                "Perlu investigasi manual kalau berulang.",
                101
            ),
        ]
        # Sort by priority (lowest first = highest importance)
        self.rules.sort(key=lambda x: x[2])

    def generate(self, trade_data: dict) -> str:
        """Pick first matching rule, format template with trade data."""
        # Ensure all expected keys have defaults to prevent KeyError
        defaults = {
            "trade_id": "UNKNOWN",
            "asset": "?",
            "side": "?",
            "score": 0,
            "entry_price": 0.0,
            "exit_price": 0.0,
            "pnl": 0.0,
            "pnl_pct": 0.0,
            "exit_reason": "unknown",
            "max_drawdown": 0.0,
            "time_held_min": 0.0,
            "regime": "unknown",
            "trend_pct": 0.0,
            "funding_rate": 0.0,
            "realized_vol": 0.02,
            "sl_distance_pct": 0.01,
            "tp2_distance_pct": 0.01,
            "atr_pct": 0.0,
            "candle_count": 0,
            "notional": 100.0,
        }
        safe_data = {**defaults, **{k: v for k, v in trade_data.items() if v is not None}}

        for condition, template, _priority in self.rules:
            try:
                if condition(safe_data):
                    return template.format(**safe_data)
            except Exception:
                continue
        return "No autopsy pattern matched."

    def aggregate(self, trades: list) -> Counter:
        """Count autopsy pattern prefixes across a list of trades."""
        patterns = Counter()
        for t in trades:
            autopsy = getattr(t, "autopsy", "") or t.get("autopsy", "") if isinstance(t, dict) else getattr(t, "autopsy", "")
            if autopsy:
                # Extract emoji+prefix before the first colon as pattern key
                prefix = autopsy.split(":")[0].strip() if ":" in autopsy else autopsy[:30]
                patterns[prefix] += 1
        return patterns

    def get_top_insight(self, trades: list) -> str:
        """Return 1 actionable sentence from recent trade patterns."""
        agg = self.aggregate(trades)
        if not agg:
            return "No data."

        top_prefix, count = agg.most_common(1)[0]

        if "TIME-EXIT CUT WINNER" in top_prefix and count >= 3:
            return (
                f"🔥 INSIGHT: {count} winner dipotong time_exit dalam {len(trades)} trade terakhir. "
                f"Naikkan time_exit 3 menit atau trailing stop lebih agresif."
            )
        if "HIGH-VOL WIPEOUT" in top_prefix and count >= 2:
            return f"🔥 INSIGHT: {count} loss dari high-vol asset. Blacklist atau size turun 75%."
        if "HIGH-SCORE TOXIC" in top_prefix and count >= 3:
            return (
                f"🔥 INSIGHT: {count} loss dari score 65+. "
                f"Naikkan min_score ke 62 atau perketat MR guard."
            )
        if "FUNDING FADE GAGAL" in top_prefix and count >= 2:
            return (
                f"🔥 INSIGHT: {count} funding fade loss. "
                f"Perketat SL 0.8× atau skip contrarian saat trend >2%."
            )
        if "SL KENA SAAT LATE TREND" in top_prefix and count >= 2:
            return (
                f"🔥 INSIGHT: {count} SL kena saat chase trend. "
                f"Late-trend filter harus lebih agresif atau size turun 50%."
            )

        return f"Top pattern: {top_prefix} ({count}x) — monitor terus."


# Singleton instance — import from anywhere
autopsy_engine = AutopsyEngine()
