"""
KARA Bot - Bitget Executor (USDT-M Futures)

Real-money executor untuk Bitget. Implement BaseExecutor interface jadi
plug-in penuh ke main.py loop dan RiskManager — keduanya tidak perlu
tahu kalau ini Bitget vs Hyperliquid.

Flow per trade:
1. signal datang (dengan harga HL)
2. PriceBridge.adjust_signal_to_bitget() → signal baru dengan harga Bitget
3. RiskManager.pre_trade_check + calculate_position_size
4. Cap leverage user config vs Bitget per-asset max
5. set_margin_mode("isolated") + set_leverage(hold_side) — boleh paralel
6. place_order limit (post_only kalau bisa, atau IOC)
7. tunggu fill (poll order detail max ~6s)
8. place_tpsl_order untuk SL on-exchange — safety net jika bot crash
9. simpan posisi shadow

Update positions:
- prices dari PriceBridge.get_bitget_price (WS cache prefer)
- RiskManager.check_tp_trail → action
- Partial close pakai close order (reduceOnly=YES)

Hedge mode mapping (Bitget default):
  LONG  open:  side=buy,  tradeSide=open
  LONG  close: side=sell, tradeSide=close
  SHORT open:  side=sell, tradeSide=open
  SHORT close: side=buy,  tradeSide=close
"""

from __future__ import annotations
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from config import RISK, EXEC
from models.schemas import (
    Order, Position, AccountState, TradeSignal, Side,
    OrderStatus, PositionStatus, BotMode, ExecutionMode,
)
from risk.risk_manager import RiskManager
from execution.base_executor import BaseExecutor
from utils.helpers import gen_id, format_usd, utcnow
from utils.excel_logger import get_excel_logger

log = logging.getLogger("kara.bitget_exec")


class BitgetExecutor(BaseExecutor):
    """
    Bitget USDT-M futures executor.

    Args:
        chat_id: user identifier
        bitget_client: BitgetClient (sudah connect, dengan credentials user)
        symbol_registry: SymbolRegistry
        price_bridge: PriceBridge untuk harga adjust + execution price
        risk_manager: RiskManager (exchange-agnostic)
        user_max_leverage: hard cap dari user config (akan di-min vs Bitget max)
    """

    def __init__(
        self,
        chat_id: str,
        bitget_client,
        symbol_registry,
        price_bridge,
        risk_manager: RiskManager,
        user_max_leverage: int = 20,
    ):
        self.chat_id    = str(chat_id)
        self.bitget     = bitget_client
        self.registry   = symbol_registry
        self.bridge     = price_bridge
        self.risk       = risk_manager
        self.user_max_leverage = user_max_leverage
        self.mode       = BotMode.LIVE

        # Local shadow of positions (synced from exchange)
        self._positions: Dict[str, Position] = {}
        # Map position_id → bitget tpsl order_id (untuk cancel saat close)
        self._tpsl_order_ids: Dict[str, str] = {}
        self._exchange_synced = False

        # Position mode di-set on first init only
        self._position_mode_set = False

        log.warning(
            f"[BITGET] Executor initialized for {self.chat_id} — REAL MONEY MODE on Bitget"
        )

    # ──────────────────────────────────────────────────────────────
    # STARTUP SYNC
    # ──────────────────────────────────────────────────────────────

    async def sync_positions_from_chain(self) -> None:
        """Recover open positions dari Bitget API setelah restart."""
        try:
            # Set hedge mode (best-effort, idempotent)
            if not self._position_mode_set:
                try:
                    await self.bitget.set_position_mode("hedge_mode")
                except Exception as e:
                    log.debug(f"[BITGET] set hedge mode skipped: {e}")
                self._position_mode_set = True

            positions = await self.bitget.get_open_positions()
            recovered = 0
            for pos_data in positions:
                bitget_sym = pos_data.get("symbol", "")
                hl_asset = self.registry.get_hl_asset(bitget_sym)
                if not hl_asset:
                    # Asset tidak kita track — skip (mungkin posisi manual user)
                    continue

                size = float(pos_data.get("total", 0) or 0)
                if size <= 0:
                    continue

                hold_side = pos_data.get("holdSide", "long").lower()
                side      = Side.LONG if hold_side == "long" else Side.SHORT
                entry_px  = float(pos_data.get("openPriceAvg") or pos_data.get("averageOpenPrice") or 0)
                upnl      = float(pos_data.get("unrealizedPL") or 0)
                lev       = int(float(pos_data.get("leverage") or 1))

                # Konversi Bitget size → HL contracts kalau k-prefix
                multiplier = self.registry.get_contract_multiplier(hl_asset)
                hl_contracts = size / multiplier if multiplier > 1 else size

                # Default SL/TP setelah recovery (best-effort, conservative 3%)
                sl_pct = 0.03
                stop_loss = (
                    entry_px * (1 - sl_pct) if side == Side.LONG
                    else entry_px * (1 + sl_pct)
                )
                margin = (size * entry_px) / max(lev, 1)

                pos = Position(
                    position_id=gen_id("REC"),  # REC = recovered
                    asset=hl_asset,
                    side=side,
                    entry_price=entry_px,
                    size_initial=hl_contracts,
                    size_current=hl_contracts,
                    leverage=lev,
                    margin_usd=margin,
                    stop_loss=stop_loss,
                    tp1=entry_px * (1.014 if side == Side.LONG else 0.986),
                    tp2=entry_px * (1.025 if side == Side.LONG else 0.975),
                    trailing_high=entry_px,
                    is_paper=False,
                    pnl_unrealized=upnl,
                )
                self._positions[pos.position_id] = pos

                # Place safety SL on Bitget supaya tidak orphan kalau bot crash lagi
                await self._place_exchange_sl(pos)
                recovered += 1
                log.warning(
                    f"[BITGET] Recovered: {hl_asset} {side.value.upper()} "
                    f"size={hl_contracts:.6f} entry={entry_px} (SL safety {stop_loss:.4f} dipasang)"
                )

            self._exchange_synced = True
            if recovered:
                log.warning(f"[BITGET] Sync complete: recovered {recovered} position(s)")
            else:
                log.info("[BITGET] Sync complete: no open positions")
        except Exception as e:
            log.error(f"[BITGET] sync_positions_from_chain failed: {e}", exc_info=True)

    # ──────────────────────────────────────────────────────────────
    # ACCOUNT STATE
    # ──────────────────────────────────────────────────────────────

    async def get_account_state(self) -> AccountState:
        try:
            acct = await self.bitget.get_account()
        except Exception as e:
            log.error(f"[BITGET] get_account failed: {e}")
            raise RuntimeError(f"Failed to fetch Bitget account: {e}")

        # Bitget v2 account fields
        total_equity   = float(acct.get("accountEquity") or acct.get("equity") or 0)
        available      = float(acct.get("crossedMaxAvailable") or acct.get("available") or 0)
        used_margin    = float(acct.get("usedMargin") or 0)
        unrealized     = float(acct.get("unrealizedPL") or 0)
        wallet_balance = total_equity - unrealized

        # Update shadow PnL dari fresh exchange data (best-effort)
        try:
            ex_positions = await self.bitget.get_open_positions()
            ex_map = {p.get("symbol", ""): p for p in ex_positions}
            for pos in self._positions.values():
                if pos.status != PositionStatus.OPEN:
                    continue
                bs = self.registry.get_bitget_symbol(pos.asset)
                if bs and bs in ex_map:
                    pos.pnl_unrealized = float(ex_map[bs].get("unrealizedPL") or 0)
        except Exception:
            pass

        drawdown = (
            (self.risk.status["peak_balance"] - total_equity) /
            max(self.risk.status["peak_balance"], 1)
        )

        return AccountState(
            total_equity=round(total_equity, 2),
            wallet_balance=round(wallet_balance, 2),
            available=round(available, 2),
            used_margin=round(used_margin, 2),
            unrealized_pnl=round(unrealized, 2),
            daily_pnl=round(self.risk.status["daily_pnl"], 2),
            daily_pnl_pct=round(self.risk.status["daily_pnl"] / max(total_equity, 1), 4),
            peak_balance=round(self.risk.status["peak_balance"], 2),
            current_drawdown_pct=round(drawdown, 4),
            positions=list(self.open_positions),
            mode=BotMode.LIVE,
            execution_mode=ExecutionMode.SEMI_AUTO,
            is_paused=self.risk.status["paused"],
            kill_switch_active=self.risk.status["kill_switch"],
        )

    @property
    def open_positions(self) -> List[Position]:
        return [p for p in self._positions.values() if p.status == PositionStatus.OPEN]

    # ──────────────────────────────────────────────────────────────
    # OPEN POSITION
    # ──────────────────────────────────────────────────────────────

    async def open_position(self, signal: TradeSignal) -> Optional[Position]:
        """Open posisi di Bitget. Signal asli sudah pakai harga HL — adjust ke Bitget dulu."""
        # 1. Adjust signal ke harga Bitget
        bridged = await self.bridge.adjust_signal_to_bitget(signal)
        if bridged is None:
            log.info(f"[BITGET] {signal.asset}: signal di-skip oleh PriceBridge")
            return None

        # 2. Risk check (pakai bridged signal supaya level konsisten)
        account = await self.get_account_state()
        approved, reason = self.risk.pre_trade_check(bridged, account, self.open_positions)
        if not approved:
            log.warning(f"[BITGET] trade blocked: {reason}")
            return None

        # 3. Cap leverage user vs Bitget max per asset
        bitget_max_lev = self.registry.get_max_leverage(signal.asset)
        capped_lev = min(
            bridged.suggested_leverage,
            self.user_max_leverage,
            bitget_max_lev,
        )
        if capped_lev != bridged.suggested_leverage:
            log.info(
                f"[BITGET] {signal.asset} leverage capped: signal={bridged.suggested_leverage}x "
                f"user_max={self.user_max_leverage}x bitget_max={bitget_max_lev}x → {capped_lev}x"
            )
            bridged.suggested_leverage = capped_lev

        # 4. Calculate position size
        size_usd, hl_contracts, actual_lev = self.risk.calculate_position_size(
            bridged, account.total_equity
        )
        if size_usd <= 0 or hl_contracts <= 0:
            log.warning(f"[BITGET] size 0 untuk {signal.asset}, skip")
            return None

        # Cap final leverage juga
        actual_lev = min(actual_lev, capped_lev)

        # 5. Konversi HL contracts → Bitget contracts
        bitget_sym = self.registry.get_bitget_symbol(signal.asset)
        multiplier = self.registry.get_contract_multiplier(signal.asset)
        bitget_size = hl_contracts * multiplier

        # Round ke step Bitget (best-effort — Bitget akan reject kalau invalid)
        info = self.registry.get_symbol_info(signal.asset)
        vol_place = info.get("vol_place", 4)
        try:
            bitget_size = round(bitget_size, int(vol_place))
        except (ValueError, TypeError):
            pass

        # Cek minimum order size
        min_qty = float(info.get("min_qty", 0))
        if min_qty > 0 and bitget_size < min_qty:
            log.warning(
                f"[BITGET] {signal.asset}: size {bitget_size} < min {min_qty} — skip"
            )
            return None

        # 6. Set margin mode + leverage (paralel — Bitget allow concurrent)
        hold_side = "long" if signal.side == Side.LONG else "short"
        try:
            await asyncio.gather(
                self.bitget.set_margin_mode(bitget_sym, "isolated"),
                self.bitget.set_leverage(bitget_sym, actual_lev, hold_side, "isolated"),
                return_exceptions=True,
            )
        except Exception as e:
            log.error(f"[BITGET] {signal.asset}: set margin/leverage failed: {e}")
            return None

        # 7. Place order — Maker-first cascade untuk fee economics:
        #
        #   [BLOCKER FIX 2026-05-17] Bitget taker fee 0.06% (round-trip 0.12%) menggerus EV
        #   strategi paper (-0.030%) jadi -0.15%/trade. Maker fee 0.02% (round-trip 0.04%)
        #   menyelamatkan 8 bps per trade. Cascade: post_only → IOC → market.
        #
        # Step A: post_only limit di harga slightly conservative (di dalam spread).
        #         Wait fill 3 detik. Kalau fill → maker fee. Kalau tidak → cancel.
        # Step B: kalau A miss, IOC limit aggressive (di luar spread, slippage 5 bps).
        #         Eksekusi instant, partial fill OK, sisa di-cancel otomatis (taker).
        # Step C: kalau IOC juga 0 fill, last resort market order (rare — biasanya cuma
        #         saat market gap besar, dan trade ini memang butuh fill cepat).
        is_long = signal.side == Side.LONG
        order_side = "buy" if is_long else "sell"

        # Post-only price: SLIGHTLY INSIDE spread supaya berperan sebagai resting liquidity.
        # Long buy = di bawah mark sedikit (offer to buy, jadi market sell ke kita = maker).
        # Short sell = di atas mark sedikit (offer to sell).
        post_only_buffer = 0.0002  # 2 bps inside spread
        post_only_px = (
            bridged.entry_price * (1 - post_only_buffer) if is_long
            else bridged.entry_price * (1 + post_only_buffer)
        )

        # IOC fallback price: slightly aggressive ke seberang spread (taker, instant fill)
        ioc_buffer = 0.0005  # 5 bps cross-spread
        ioc_px = (
            bridged.entry_price * (1 + ioc_buffer) if is_long
            else bridged.entry_price * (1 - ioc_buffer)
        )

        try:
            price_place = int(info.get("price_place", 4) or 4)
            post_only_px = round(post_only_px, price_place)
            ioc_px = round(ioc_px, price_place)
        except (ValueError, TypeError):
            post_only_px = round(post_only_px, 4)
            ioc_px = round(ioc_px, 4)

        order_resp = None
        used_path = None

        # ── Step A: Post-only (maker fee) ──────────────────────────────────
        po_oid = gen_id("BG-PO")
        try:
            order_resp = await self.bitget.place_order(
                symbol=bitget_sym,
                side=order_side,
                order_type="limit",
                size=bitget_size,
                trade_side="open",
                price=post_only_px,
                client_oid=po_oid,
                force="post_only",
            )
            used_path = "post_only"
        except Exception as e:
            log.warning(f"[BITGET] {signal.asset}: post_only place_order failed: {e}")

        # Wait fill window 3 detik untuk post-only
        filled_size = 0.0
        fill_price = 0.0
        if order_resp:
            order_id_a = order_resp.get("orderId") or order_resp.get("clientOid") or po_oid
            filled_size, fill_price = await self._wait_for_fill(bitget_sym, order_id_a, max_wait_s=3.0)

            # Kalau post-only tidak fill (atau partial), cancel sisanya supaya
            # tidak nyangkut. Pakai size partial yang sudah fill — sisa di-IOC.
            if filled_size <= 0:
                try:
                    await self.bitget.cancel_order(bitget_sym, order_id=order_id_a)
                except Exception as ce:
                    log.debug(f"[BITGET] cancel post-only {order_id_a} failed: {ce}")

        # ── Step B: IOC fallback (taker fee) ───────────────────────────────
        if filled_size <= 0:
            log.info(f"[BITGET] {signal.asset}: post-only miss, fallback IOC")
            ioc_oid = gen_id("BG-IOC")
            try:
                ioc_resp = await self.bitget.place_order(
                    symbol=bitget_sym,
                    side=order_side,
                    order_type="limit",
                    size=bitget_size,
                    trade_side="open",
                    price=ioc_px,
                    client_oid=ioc_oid,
                    force="ioc",
                )
                used_path = "ioc"
                ioc_order_id = ioc_resp.get("orderId") or ioc_resp.get("clientOid") or ioc_oid
                filled_size, fill_price = await self._wait_for_fill(bitget_sym, ioc_order_id, max_wait_s=4.0)
            except Exception as e:
                log.warning(f"[BITGET] {signal.asset}: IOC fallback failed: {e}")

        # ── Step C: Market last resort ──────────────────────────────────────
        if filled_size <= 0:
            log.warning(f"[BITGET] {signal.asset}: IOC tidak fill — last resort market")
            try:
                mkt_oid = gen_id("BG-MKT")
                await self.bitget.place_order(
                    symbol=bitget_sym,
                    side=order_side,
                    order_type="market",
                    size=bitget_size,
                    trade_side="open",
                    client_oid=mkt_oid,
                )
                used_path = "market"
                filled_size, fill_price = await self._wait_for_fill(bitget_sym, mkt_oid, max_wait_s=4.0)
            except Exception as e:
                log.error(f"[BITGET] {signal.asset}: market fallback failed: {e}")
                return None

        if filled_size <= 0:
            log.error(f"[BITGET] {signal.asset}: order tidak fill via all paths, abort")
            return None

        log.info(
            f"[BITGET] {signal.asset}: order filled via {used_path.upper()} "
            f"@ {fill_price} (size={filled_size})"
        )

        # Konversi filled Bitget size kembali ke HL contracts
        filled_hl_contracts = filled_size / multiplier if multiplier > 1 else filled_size

        # 9. Build position record
        margin = (filled_size * fill_price) / max(actual_lev, 1)
        pos = Position(
            position_id=gen_id("POS"),
            asset=signal.asset,
            side=signal.side,
            entry_price=fill_price,
            size_initial=filled_hl_contracts,
            size_current=filled_hl_contracts,
            leverage=actual_lev,
            margin_usd=margin,
            stop_loss=bridged.stop_loss,
            tp1=bridged.tp1,
            tp2=bridged.tp2,
            tp3=getattr(bridged, "tp3", 0.0),
            trailing_high=fill_price,
            trailing_stop_price=0.0,
            entry_atr=getattr(bridged, "entry_atr", 0.0),
            signal_id=bridged.signal_id,
            is_paper=False,
            entry_score=bridged.score,
            entry_tier=getattr(bridged, 'v10_tier', 'B'),
            realized_vol=getattr(bridged, "realized_vol", 0.02),
            original_entry_price=fill_price,
            entry_funding_rate=getattr(bridged, "funding_rate", 0.0) or 0.0,
            atr_pct=getattr(bridged, "entry_atr", 0.0),
        )
        self._positions[pos.position_id] = pos

        # 10. Place on-exchange SL — safety net jika bot mati
        await self._place_exchange_sl(pos)

        # 11. Log
        log_data = {
            "type":        "open",
            "exchange":    "bitget",
            "fill_path":   used_path,   # post_only | ioc | market — untuk audit fee economics
            "pos_id":      pos.position_id,
            "asset":       signal.asset,
            "bitget_sym":  bitget_sym,
            "side":        signal.side.value,
            "entry_price": fill_price,
            "mark_price":  bridged.entry_price,
            "contracts":   filled_hl_contracts,
            "bitget_size": filled_size,
            "notional":    filled_size * fill_price,
            "leverage":    actual_lev,
            "score":       signal.score,
            "timestamp":   utcnow(),
        }
        get_excel_logger().log_trade(self.chat_id, log_data)
        self.risk.record_asset_trade(signal.asset)

        log.info(
            f"[BITGET] OPEN {signal.asset} {signal.side.value.upper()} "
            f"@ {fill_price:.6f} (HL ref={signal.entry_price:.6f}) | "
            f"{filled_hl_contracts:.6f} HL ctr ({filled_size:.4f} BG ctr) | "
            f"{actual_lev}x isolated"
        )
        return pos

    async def _wait_for_fill(
        self, bitget_sym: str, order_id: str, max_wait_s: float = 6.0
    ) -> Tuple[float, float]:
        """Poll order detail sampai filled atau timeout. Return (filled_size, avg_price)."""
        deadline = asyncio.get_event_loop().time() + max_wait_s
        last_detail: Dict = {}

        while asyncio.get_event_loop().time() < deadline:
            try:
                detail = await self.bitget.get_order(bitget_sym, order_id)
                if detail:
                    last_detail = detail
                    status = (detail.get("state") or detail.get("status") or "").lower()
                    filled = float(detail.get("baseVolume") or detail.get("filledQty") or 0)
                    avg_px = float(detail.get("priceAvg") or detail.get("fillPrice") or 0)
                    if status in ("filled", "full_fill") and filled > 0:
                        return filled, avg_px
                    if status in ("cancelled", "canceled") and filled > 0:
                        # Partial fill before cancel — masih bisa pakai
                        return filled, avg_px
                    if status in ("cancelled", "canceled") and filled <= 0:
                        return 0.0, 0.0
            except Exception:
                pass
            await asyncio.sleep(0.3)

        # Timeout — return whatever we got last
        if last_detail:
            filled = float(last_detail.get("baseVolume") or last_detail.get("filledQty") or 0)
            avg_px = float(last_detail.get("priceAvg") or last_detail.get("fillPrice") or 0)
            return filled, avg_px
        return 0.0, 0.0

    # ──────────────────────────────────────────────────────────────
    # ON-EXCHANGE SL MANAGEMENT
    # ──────────────────────────────────────────────────────────────

    async def _place_exchange_sl(self, pos: Position) -> None:
        """
        Bitget pos-level SL — auto-resize jika partial close. Aktif di server
        meski bot mati, jadi proteksi modal tetap ada.
        """
        bitget_sym = self.registry.get_bitget_symbol(pos.asset)
        if not bitget_sym or pos.stop_loss <= 0:
            return
        hold_side = "long" if pos.side == Side.LONG else "short"
        try:
            resp = await self.bitget.place_tpsl_order(
                symbol=bitget_sym,
                plan_type="pos_loss",
                trigger_price=pos.stop_loss,
                hold_side=hold_side,
                client_oid=gen_id("SL"),
            )
            sl_oid = resp.get("orderId") or ""
            if sl_oid:
                self._tpsl_order_ids[pos.position_id] = sl_oid
            log.info(f"[BITGET-SL] {pos.asset} SL @ {pos.stop_loss:.6f} dipasang (oid={sl_oid})")
        except Exception as e:
            log.warning(f"[BITGET-SL] {pos.asset}: failed place SL: {e} (software SL tetap aktif)")

    async def _cancel_exchange_sl(self, pos: Position) -> None:
        sl_oid = self._tpsl_order_ids.pop(pos.position_id, None)
        if not sl_oid:
            return
        bitget_sym = self.registry.get_bitget_symbol(pos.asset)
        if not bitget_sym:
            return
        try:
            await self.bitget.cancel_tpsl_order(
                symbol=bitget_sym,
                order_id_list=[{"orderId": sl_oid}],
            )
        except Exception as e:
            log.debug(f"[BITGET-SL] cancel SL {sl_oid} failed: {e}")

    async def update_exchange_sl(self, pos: Position, new_sl_price: float) -> None:
        await self._cancel_exchange_sl(pos)
        pos.stop_loss = new_sl_price
        await self._place_exchange_sl(pos)

    # ──────────────────────────────────────────────────────────────
    # UPDATE POSITIONS (monitor loop)
    # ──────────────────────────────────────────────────────────────

    async def update_positions(self, prices: Dict[str, float]) -> List[Dict]:
        """
        Per-tick monitor. `prices` dipasok caller dengan Bitget mark price
        per asset (caller = main.py loop, pakai PriceBridge / WS cache).
        """
        actions: List[Dict] = []
        for pos_id, pos in list(self._positions.items()):
            if pos.status != PositionStatus.OPEN:
                continue
            current = prices.get(pos.asset, 0)
            if current <= 0:
                continue

            pos.pnl_unrealized = pos.unrealized_pnl(current)
            if pos.pnl_unrealized < pos.max_unrealized_loss:
                pos.max_unrealized_loss = pos.pnl_unrealized

            # Cek RiskManager triggers
            action = self.risk.check_tp_trail(pos, current)
            if action and pos.status == PositionStatus.OPEN:
                result = await self._execute_action(pos, action, current)
                if result:
                    actions.append(result)
        return actions

    async def _execute_action(
        self, pos: Position, action: Dict, current_price: float
    ) -> Optional[Dict]:
        atype = action.get("action", "")
        close_ratio = float(action.get("close_ratio", 1.0))

        # Full close untuk SL/trail/time/momentum/early_trail
        if atype in ("trailing_stop", "stop_loss", "time_exit", "progress_stop", "momentum_exit", "early_trail"):
            fill_px = pos.stop_loss if atype == "stop_loss" else current_price
            res = await self.close_position(pos.position_id, fill_px, reason=atype)
            return {**action, "pnl": (res or {}).get("pnl", 0), "position_id": pos.position_id}

        # Partial close untuk TP1 / TP2 / TP3
        res = await self._do_close(pos, close_ratio, current_price, reason=atype)
        if not res:
            return None

        if atype == "tp1":
            pos.tp1_hit = True
            pos.trailing_active = True
            pos.trailing_high = current_price
            be_ref = pos.original_entry_price or pos.entry_price
            new_sl = be_ref * 1.001 if pos.side == Side.LONG else be_ref * 0.999
            await self.update_exchange_sl(pos, new_sl)
            if "tp1" not in pos.partial_exits_done:
                pos.partial_exits_done.append("tp1")
            log.info(f"[BITGET] TP1 hit {pos.asset} → SL ke breakeven {new_sl:.6f}")
        elif atype == "tp2":
            pos.tp2_hit = True
            if "tp2" not in pos.partial_exits_done:
                pos.partial_exits_done.append("tp2")
        elif atype == "tp3":
            pos.tp3_hit = True
            if "tp3" not in pos.partial_exits_done:
                pos.partial_exits_done.append("tp3")

        if pos.side == Side.LONG:
            pos.trailing_high = max(pos.trailing_high, current_price)
        else:
            pos.trailing_high = min(pos.trailing_high, current_price) if pos.trailing_high > 0 else current_price

        return {**action, "pnl": res.get("pnl", 0), "position_id": pos.position_id}

    async def _do_close(
        self, pos: Position, ratio: float, current_price: float, reason: str
    ) -> Optional[Dict]:
        """Internal: kirim close order ke Bitget untuk `ratio` dari size_current."""
        bitget_sym = self.registry.get_bitget_symbol(pos.asset)
        if not bitget_sym:
            return None

        # Konversi size HL → Bitget
        multiplier = self.registry.get_contract_multiplier(pos.asset)
        close_hl  = pos.size_current * ratio
        close_bg  = close_hl * multiplier

        info = self.registry.get_symbol_info(pos.asset)
        try:
            close_bg = round(close_bg, int(info.get("vol_place", 4)))
        except (ValueError, TypeError):
            pass

        if close_bg <= 0:
            return None

        # Hedge mode close
        order_side = "sell" if pos.side == Side.LONG else "buy"
        try:
            await self.bitget.place_order(
                symbol=bitget_sym,
                side=order_side,
                order_type="market",
                size=close_bg,
                trade_side="close",
                reduce_only=True,
                client_oid=gen_id("CL"),
            )
        except Exception as e:
            log.error(f"[BITGET] close {pos.asset} failed: {e}")
            return None

        # Estimate PnL (Bitget reports actual via WS — kita pakai approximation untuk shadow)
        pnl = pos.floating_pct(current_price) * close_hl * pos.entry_price
        pos.pnl_realized += pnl
        pos.size_current -= close_hl

        log_data = {
            "type":         "close",
            "exchange":     "bitget",
            "pos_id":       pos.position_id,
            "asset":        pos.asset,
            "side":         pos.side.value,
            "reason":       reason,
            "ratio":        ratio,
            "entry_price":  pos.entry_price,
            "exit_price":   current_price,
            "size":         close_hl,
            "bitget_size":  close_bg,
            "notional":     close_hl * current_price,
            "pnl":          pnl,
            "pnl_pct":      pos.roe_pct(current_price),
            "score":        pos.entry_score,
            "timestamp":    utcnow(),
        }
        get_excel_logger().log_trade(self.chat_id, log_data)

        log.info(
            f"[BITGET] CLOSE {ratio*100:.0f}% {pos.asset} @ {current_price:.6f} | "
            f"PnL: {format_usd(pnl)} ({reason})"
        )
        return {"position_id": pos.position_id, "pnl": pnl, "reason": reason}

    async def close_position(
        self,
        position_id: str,
        current_price: float,
        reason: str = "manual",
    ) -> Optional[Dict]:
        pos = self._positions.get(position_id)
        if not pos or pos.status == PositionStatus.CLOSED:
            return None

        res = await self._do_close(pos, 1.0, current_price, reason=reason)
        if not res:
            return None

        pos.status = PositionStatus.CLOSED
        pos.closed_at = utcnow()
        await self._cancel_exchange_sl(pos)

        # Record PnL untuk daily guard
        try:
            acct = await self.get_account_state()
            self.risk.record_pnl(pos.pnl_realized, acct.total_equity)
        except Exception:
            pass

        # Save trade history
        try:
            from core.db import user_db
            user_db.save_trade(self.chat_id, {
                "exchange":   "bitget",
                "pos_id":     position_id,
                "asset":      pos.asset,
                "side":       pos.side.value,
                "reason":     reason,
                "entry_price": pos.entry_price,
                "exit_price": current_price,
                "size":       pos.size_initial,
                "notional":   pos.size_initial * pos.entry_price,
                "pnl":        pos.pnl_realized,
                "pnl_pct":    pos.roe_pct(current_price),
                "score":      pos.entry_score,
                "tier":       getattr(pos, 'entry_tier', 'B'),
                "timestamp":  utcnow(),
            })
        except Exception:
            pass

        return res
