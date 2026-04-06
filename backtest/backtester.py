"""
KARA Bot - Vectorized Backtesting Pipeline 
Simple but realistic backtesting using historical OHLCV data.
Supports: signal replay, TP1/TP2/trailing simulation, drawdown analysis.
"""

from __future__ import annotations
import csv
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import config
from config import RISK, BACKTEST_INITIAL_CAPITAL

log = logging.getLogger("kara.backtest")


@dataclass
class BacktestTrade:
    asset:        str
    side:         str                  # "long" | "short"
    entry_ts:     datetime
    entry_price:  float
    size_usd:     float
    stop_loss:    float
    tp1:          float
    tp2:          float
    exit_ts:      Optional[datetime] = None
    exit_price:   float = 0.0
    pnl_usd:      float = 0.0
    pnl_pct:      float = 0.0
    exit_reason:  str = ""            # "tp1" | "tp2" | "trailing" | "stop_loss"
    score_at_entry: int = 0


@dataclass
class BacktestResult:
    asset:             str
    start_date:        str
    end_date:          str
    initial_capital:   float
    final_capital:     float
    total_return_pct:  float
    total_trades:      int
    win_rate:          float
    avg_win_pct:       float
    avg_loss_pct:      float
    profit_factor:     float
    max_drawdown_pct:  float
    sharpe_ratio:      float
    trades:            List[BacktestTrade] = field(default_factory=list)

    def summary_text(self) -> str:
        return (
            f" BACKTEST RESULTS - {self.asset}\n"
            f"Period: {self.start_date} -> {self.end_date}\n"
            f"{'─'*40}\n"
            f"Initial Capital:  ${self.initial_capital:,.2f}\n"
            f"Final Capital:    ${self.final_capital:,.2f}\n"
            f"Total Return:     {self.total_return_pct:+.1f}%\n"
            f"Total Trades:     {self.total_trades}\n"
            f"Win Rate:         {self.win_rate*100:.1f}%\n"
            f"Avg Win:          {self.avg_win_pct*100:+.2f}%\n"
            f"Avg Loss:         {self.avg_loss_pct*100:.2f}%\n"
            f"Profit Factor:    {self.profit_factor:.2f}\n"
            f"Max Drawdown:     {self.max_drawdown_pct*100:.1f}%\n"
            f"Sharpe Ratio:     {self.sharpe_ratio:.2f}\n"
        )


class VectorizedBacktester:
    """
    Simple vectorized backtester.
    Loads OHLCV CSV, simulates signal -> entry -> TP/SL logic.
    
    CSV format expected:
    timestamp,open,high,low,close,volume
    (UTC Unix timestamp in ms or ISO datetime)
    """

    def __init__(self, data_dir: str = "backtest/data"):
        self.data_dir = data_dir

    def run(
        self,
        asset: str,
        candles: List[Dict],   # [{ts, open, high, low, close, volume}, ...]
        signals: List[Dict],   # [{ts, side, entry, sl, tp1, tp2, score}, ...]
        leverage: int = 5,
        risk_pct: float = 0.025,
    ) -> BacktestResult:
        """
        Run backtest on pre-computed signals against historical candles.
        Vectorized: processes all candles in a single loop pass.
        """
        if not candles or not signals:
            raise ValueError("Need candles and signals to backtest")

        # Align signals to candles (signal must precede entry candle)
        balance      = BACKTEST_INITIAL_CAPITAL
        peak_balance = balance
        max_drawdown = 0.0
        trades: List[BacktestTrade] = []
        active_trade: Optional[BacktestTrade] = None
        signal_idx   = 0
        sorted_sigs  = sorted(signals, key=lambda s: s["ts"])

        for candle in candles:
            c_ts    = candle["ts"]
            c_open  = float(candle["open"])
            c_high  = float(candle["high"])
            c_low   = float(candle["low"])
            c_close = float(candle["close"])

            # ── Try to open a new trade from signal ────────────────────
            if active_trade is None and signal_idx < len(sorted_sigs):
                sig = sorted_sigs[signal_idx]
                if sig["ts"] <= c_ts:
                    # Enter at open of next candle
                    entry    = c_open
                    sl_pct   = abs(entry - sig["sl"]) / entry
                    if sl_pct > 0:
                        size_usd  = (balance * risk_pct) / (sl_pct * leverage)
                        size_usd  = min(size_usd, balance * 0.5)   # max 50% balance
                        active_trade = BacktestTrade(
                            asset=asset,
                            side=sig["side"],
                            entry_ts=datetime.utcfromtimestamp(c_ts / 1000),
                            entry_price=entry,
                            size_usd=size_usd,
                            stop_loss=sig["sl"] * (entry / sig["entry"]),   # rescale
                            tp1=sig["tp1"] * (entry / sig["entry"]),
                            tp2=sig["tp2"] * (entry / sig["entry"]),
                            score_at_entry=sig.get("score", 0),
                        )
                    signal_idx += 1

            # ── Check active trade against this candle ─────────────────
            if active_trade is not None:
                result = self._check_trade_candle(active_trade, c_high, c_low, c_close, c_ts)
                if result:
                    # Trade closed
                    pnl_pct = active_trade.pnl_pct
                    pnl_usd = active_trade.size_usd * pnl_pct * leverage

                    balance += pnl_usd
                    active_trade.pnl_usd = pnl_usd

                    # Update drawdown
                    if balance > peak_balance:
                        peak_balance = balance
                    drawdown = (peak_balance - balance) / peak_balance
                    max_drawdown = max(max_drawdown, drawdown)

                    trades.append(active_trade)
                    active_trade = None

                    # Daily loss kill-switch simulation
                    if drawdown > RISK.max_drawdown_pct:
                        log.warning(f"Backtest: max drawdown {drawdown:.1%} hit - stopping")
                        break

        # ── Compute stats ──────────────────────────────────────────────
        return self._compute_stats(
            asset=asset,
            trades=trades,
            initial_capital=BACKTEST_INITIAL_CAPITAL,
            final_capital=balance,
            max_drawdown=max_drawdown,
        )

    def _check_trade_candle(
        self,
        trade: BacktestTrade,
        high: float,
        low: float,
        close: float,
        ts: int,
    ) -> bool:
        """
        Check if SL or TP was hit in this candle.
        Returns True if trade should be closed.
        Handles partial TP (simplified: tp1 and tp2 as single exit in backtest).
        """
        if trade.side == "long":
            # Stop loss
            if low <= trade.stop_loss:
                trade.exit_price  = trade.stop_loss
                trade.pnl_pct     = (trade.stop_loss - trade.entry_price) / trade.entry_price
                trade.exit_reason = "stop_loss"
                trade.exit_ts     = datetime.utcfromtimestamp(ts / 1000)
                return True
            # TP2 (simplified combined exit)
            if high >= trade.tp2:
                # Partial TP simulation:
                # 40% at tp1, 35% at tp2, 25% at close (trailing approx)
                tp1_pnl = (trade.tp1 - trade.entry_price) / trade.entry_price * 0.40
                tp2_pnl = (trade.tp2 - trade.entry_price) / trade.entry_price * 0.35
                trail_pnl = (close - trade.entry_price) / trade.entry_price * 0.25
                trade.pnl_pct     = tp1_pnl + tp2_pnl + trail_pnl
                trade.exit_price  = close
                trade.exit_reason = "tp2+trailing"
                trade.exit_ts     = datetime.utcfromtimestamp(ts / 1000)
                return True
            # TP1 only
            if high >= trade.tp1 and not getattr(trade, '_tp1_done', False):
                trade._tp1_done = True   # type: ignore
                # Not closing yet; move stop to breakeven
                trade.stop_loss = trade.entry_price

        else:  # SHORT
            if high >= trade.stop_loss:
                trade.exit_price  = trade.stop_loss
                trade.pnl_pct     = (trade.entry_price - trade.stop_loss) / trade.entry_price
                trade.exit_reason = "stop_loss"
                trade.exit_ts     = datetime.utcfromtimestamp(ts / 1000)
                return True
            if low <= trade.tp2:
                tp1_pnl = (trade.entry_price - trade.tp1) / trade.entry_price * 0.40
                tp2_pnl = (trade.entry_price - trade.tp2) / trade.entry_price * 0.35
                trail_pnl = (trade.entry_price - close) / trade.entry_price * 0.25
                trade.pnl_pct     = tp1_pnl + tp2_pnl + trail_pnl
                trade.exit_price  = close
                trade.exit_reason = "tp2+trailing"
                trade.exit_ts     = datetime.utcfromtimestamp(ts / 1000)
                return True
            if low <= trade.tp1:
                trade._tp1_done = True  # type: ignore
                trade.stop_loss = trade.entry_price

        return False

    def _compute_stats(
        self,
        asset: str,
        trades: List[BacktestTrade],
        initial_capital: float,
        final_capital: float,
        max_drawdown: float,
    ) -> BacktestResult:
        if not trades:
            return BacktestResult(
                asset=asset,
                start_date="N/A", end_date="N/A",
                initial_capital=initial_capital, final_capital=initial_capital,
                total_return_pct=0, total_trades=0, win_rate=0,
                avg_win_pct=0, avg_loss_pct=0, profit_factor=0,
                max_drawdown_pct=0, sharpe_ratio=0, trades=[]
            )

        winners = [t for t in trades if t.pnl_usd > 0]
        losers  = [t for t in trades if t.pnl_usd <= 0]

        win_rate    = len(winners) / len(trades)
        avg_win_pct = sum(t.pnl_pct for t in winners) / max(len(winners), 1)
        avg_loss_pct= sum(t.pnl_pct for t in losers)  / max(len(losers),  1)

        gross_profit = sum(t.pnl_usd for t in winners)
        gross_loss   = abs(sum(t.pnl_usd for t in losers))
        profit_factor= gross_profit / max(gross_loss, 0.01)

        # Simple Sharpe (daily returns approximation)
        daily_returns = [t.pnl_usd / initial_capital for t in trades]
        if len(daily_returns) > 1:
            avg_r   = sum(daily_returns) / len(daily_returns)
            std_r   = (sum((r - avg_r)**2 for r in daily_returns) / len(daily_returns)) ** 0.5
            sharpe  = (avg_r / max(std_r, 0.0001)) * (252 ** 0.5)   # annualized
        else:
            sharpe  = 0.0

        total_return = (final_capital - initial_capital) / initial_capital * 100

        start = trades[0].entry_ts.strftime("%Y-%m-%d") if trades else "N/A"
        end   = (trades[-1].exit_ts or trades[-1].entry_ts).strftime("%Y-%m-%d") if trades else "N/A"

        return BacktestResult(
            asset=asset,
            start_date=start,
            end_date=end,
            initial_capital=initial_capital,
            final_capital=final_capital,
            total_return_pct=round(total_return, 2),
            total_trades=len(trades),
            win_rate=round(win_rate, 4),
            avg_win_pct=round(avg_win_pct, 4),
            avg_loss_pct=round(avg_loss_pct, 4),
            profit_factor=round(profit_factor, 2),
            max_drawdown_pct=round(max_drawdown, 4),
            sharpe_ratio=round(sharpe, 2),
            trades=trades,
        )

    def save_results(self, result: BacktestResult, path: str = "backtest/results.json"):
        """Save backtest results to JSON."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {
            "asset": result.asset,
            "start_date": result.start_date,
            "end_date": result.end_date,
            "initial_capital": result.initial_capital,
            "final_capital": result.final_capital,
            "total_return_pct": result.total_return_pct,
            "total_trades": result.total_trades,
            "win_rate": result.win_rate,
            "profit_factor": result.profit_factor,
            "max_drawdown_pct": result.max_drawdown_pct,
            "sharpe_ratio": result.sharpe_ratio,
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        log.info(f"Backtest results saved -> {path}")
