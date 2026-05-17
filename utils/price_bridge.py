"""
KARA Bot - Price Bridge

Sinyal dibuat dari harga Hyperliquid. Eksekusi di Bitget. Karena harga
kedua exchange bisa berbeda (slippage cross-venue), SL/TP harus dihitung
ulang dari harga Bitget supaya level tetap konsisten secara persentase.

Tugas PriceBridge:
1. Cek availability di Bitget (skip kalau tidak ada).
2. Ambil harga Bitget current (prefer WS cache, fallback REST).
3. Cek gap HL vs Bitget — kalau > threshold, skip trade (terlalu jauh).
4. Recalculate entry/SL/TP1/TP2/TP3 pakai harga Bitget dengan PERSENTASE
   yang sama dari signal asli.
5. Cache hasil singkat untuk burst de-dupe.

Low-latency design:
- Cache 2s TTL untuk price (cocok untuk burst signal)
- WS price dipakai dulu (sub-second freshness), REST hanya fallback
- Pre-warm: caller bisa hint asset apa yang sebentar lagi mungkin signal
"""

from __future__ import annotations
import asyncio
import copy
import logging
import time
from typing import Dict, Optional, Tuple

from models.schemas import TradeSignal, Side

log = logging.getLogger("kara.price_bridge")


class PriceBridge:
    """
    Bridge harga HL → harga Bitget untuk eksekusi.

    Args:
        bitget_client: BitgetClient instance (untuk REST fallback)
        symbol_registry: SymbolRegistry untuk symbol mapping
        ws_cache: BitgetMarketDataCache untuk WS-driven price (opsional)
        max_gap_pct: Maksimum % gap HL vs Bitget — di atas ini trade di-skip
        cache_ttl_s: TTL price cache REST
    """

    def __init__(
        self,
        bitget_client,
        symbol_registry,
        ws_cache=None,
        max_gap_pct: float = 0.003,
        cache_ttl_s: float = 2.0,
    ):
        self.bitget       = bitget_client
        self.registry     = symbol_registry
        self.ws_cache     = ws_cache
        self.max_gap_pct  = max_gap_pct
        self.cache_ttl_s  = cache_ttl_s

        # symbol → (price, timestamp)
        self._rest_cache: Dict[str, Tuple[float, float]] = {}

    async def get_bitget_price(self, hl_asset: str) -> float:
        """
        Get current Bitget mark price untuk asset HL.

        Priority:
        1. WS cache (sub-second freshness, no HTTP roundtrip)
        2. REST cache (≤2s TTL)
        3. REST fetch
        """
        bitget_sym = self.registry.get_bitget_symbol(hl_asset)
        if not bitget_sym:
            return 0.0

        # ── WS cache (paling cepat) ──────────────────────────────
        if self.ws_cache is not None:
            ws_price = self.ws_cache.get_price(bitget_sym, max_age_s=5.0)
            if ws_price > 0:
                return ws_price

        # ── REST cache ───────────────────────────────────────────
        cached = self._rest_cache.get(bitget_sym)
        now = time.time()
        if cached and (now - cached[1]) < self.cache_ttl_s:
            return cached[0]

        # ── REST fetch ───────────────────────────────────────────
        try:
            price = await self.bitget.get_mark_price(bitget_sym)
            if price > 0:
                self._rest_cache[bitget_sym] = (price, now)
                # Sekalian subscribe WS supaya nanti tidak perlu REST lagi
                if self.ws_cache is not None and hasattr(self.bitget, "_ws_client"):
                    pass  # WS subscription dikelola di tempat lain
            return price
        except Exception as e:
            log.debug(f"[BRIDGE] REST fetch {bitget_sym} failed: {e}")
            return 0.0

    async def adjust_signal_to_bitget(
        self,
        signal: TradeSignal,
    ) -> Optional[TradeSignal]:
        """
        Recalculate signal levels berdasarkan harga Bitget current.

        Return None jika:
        - Asset tidak ada di Bitget
        - Harga Bitget tidak tersedia
        - Gap HL vs Bitget > max_gap_pct

        Otherwise return TradeSignal baru (deep-copied) dengan
        entry/SL/TP1/TP2/TP3 dihitung dari harga Bitget pakai
        persentase yang sama dari signal HL asli.
        """
        # 1. Check availability
        if not self.registry.is_available(signal.asset):
            log.info(f"[BRIDGE] {signal.asset}: tidak ada di Bitget, skip")
            return None

        # 2. Fetch Bitget price
        bitget_price = await self.get_bitget_price(signal.asset)
        if bitget_price <= 0:
            log.warning(f"[BRIDGE] {signal.asset}: harga Bitget tidak tersedia, skip trade")
            return None

        # 3. Check gap
        hl_price = float(signal.entry_price)
        if hl_price <= 0:
            log.warning(f"[BRIDGE] {signal.asset}: entry_price HL invalid ({hl_price})")
            return None

        gap_pct = abs(bitget_price - hl_price) / hl_price
        if gap_pct > self.max_gap_pct:
            log.warning(
                f"[BRIDGE] {signal.asset}: HL={hl_price:.6f} Bitget={bitget_price:.6f} "
                f"gap={gap_pct*100:.3f}% > {self.max_gap_pct*100:.2f}% — skip trade"
            )
            return None

        # 4. Recalculate semua level pakai harga Bitget
        adjusted = signal.model_copy(deep=True)
        is_long = signal.side == Side.LONG

        sl_pct  = abs(signal.stop_loss - hl_price) / hl_price if signal.stop_loss > 0 else 0
        tp1_pct = abs(signal.tp1       - hl_price) / hl_price if signal.tp1       > 0 else 0
        tp2_pct = abs(signal.tp2       - hl_price) / hl_price if signal.tp2       > 0 else 0
        tp3_pct = abs(signal.tp3       - hl_price) / hl_price if signal.tp3       > 0 else 0

        adjusted.entry_price = round(bitget_price, 8)

        if is_long:
            adjusted.stop_loss = round(bitget_price * (1 - sl_pct), 8)  if sl_pct > 0 else signal.stop_loss
            adjusted.tp1       = round(bitget_price * (1 + tp1_pct), 8) if tp1_pct > 0 else signal.tp1
            adjusted.tp2       = round(bitget_price * (1 + tp2_pct), 8) if tp2_pct > 0 else signal.tp2
            adjusted.tp3       = round(bitget_price * (1 + tp3_pct), 8) if tp3_pct > 0 else 0.0
        else:
            adjusted.stop_loss = round(bitget_price * (1 + sl_pct), 8)  if sl_pct > 0 else signal.stop_loss
            adjusted.tp1       = round(bitget_price * (1 - tp1_pct), 8) if tp1_pct > 0 else signal.tp1
            adjusted.tp2       = round(bitget_price * (1 - tp2_pct), 8) if tp2_pct > 0 else signal.tp2
            adjusted.tp3       = round(bitget_price * (1 - tp3_pct), 8) if tp3_pct > 0 else 0.0

        log.info(
            f"[BRIDGE] {signal.asset} {signal.side.value.upper()} | "
            f"HL={hl_price:.6f} → Bitget={bitget_price:.6f} (gap={gap_pct*100:.3f}%) | "
            f"SL pct={sl_pct*100:.2f}% TP2 pct={tp2_pct*100:.2f}%"
        )
        return adjusted

    async def prewarm(self, hl_assets) -> int:
        """
        Optional: pre-fetch harga untuk daftar asset (mis. saat startup
        atau saat scanner mulai jalan), supaya cache sudah hangat sebelum
        signal datang.
        """
        if not hl_assets:
            return 0

        # Build list of Bitget symbols
        bitget_syms = []
        for a in hl_assets:
            sym = self.registry.get_bitget_symbol(a)
            if sym:
                bitget_syms.append(sym)

        if not bitget_syms:
            return 0

        # Batch fetch (1 HTTP call)
        try:
            prices = await self.bitget.get_mark_prices_batch(bitget_syms)
            now = time.time()
            for sym, px in prices.items():
                self._rest_cache[sym] = (px, now)
            log.info(f"[BRIDGE] Prewarmed {len(prices)} prices")
            return len(prices)
        except Exception as e:
            log.warning(f"[BRIDGE] prewarm failed: {e}")
            return 0
