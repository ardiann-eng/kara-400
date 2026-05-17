"""
KARA Bot - Symbol Registry

Pemetaan symbol Hyperliquid → Bitget USDT-M perpetuals.

Hyperliquid menggunakan ticker pendek (BTC, ETH, kPEPE), sementara
Bitget memakai format <COIN>USDT (BTCUSDT, PEPEUSDT). Beberapa asset
HL pakai "k" prefix yang artinya kontrak per 1000 token — perlu
penanganan contract size khusus.

Registry ini:
1. Static mapping HL→Bitget untuk asset-asset yang sudah kita validasi.
2. Auto-discovery: pas startup cek availability di Bitget; asset yang
   tidak ada langsung di-skip dari market scanner.
3. Menyimpan contract size multiplier untuk "k" prefix assets.
"""

from __future__ import annotations
import asyncio
import logging
from typing import Dict, Optional, Set, Tuple

log = logging.getLogger("kara.symbol_registry")

# ──────────────────────────────────────────────────────────────────
# STATIC MAPPING — HL ticker → Bitget USDT-M symbol
# ──────────────────────────────────────────────────────────────────
# Aturan default:
#   <COIN>           → <COIN>USDT
#   k<COIN>          → <COIN>USDT (HL pakai per-1000 contract, Bitget per-1)
#   None             → skip (asset tidak ada di Bitget atau HL-exclusive)
HL_TO_BITGET: Dict[str, Optional[str]] = {
    # Major
    "BTC":     "BTCUSDT",
    "ETH":     "ETHUSDT",
    "SOL":     "SOLUSDT",
    "BNB":     "BNBUSDT",
    "XRP":     "XRPUSDT",
    "DOGE":    "DOGEUSDT",
    "ADA":     "ADAUSDT",
    "AVAX":    "AVAXUSDT",
    "LINK":    "LINKUSDT",
    "DOT":     "DOTUSDT",
    "UNI":     "UNIUSDT",
    "NEAR":    "NEARUSDT",
    "ARB":     "ARBUSDT",
    "OP":      "OPUSDT",
    "SUI":     "SUIUSDT",
    "APT":     "APTUSDT",
    "INJ":     "INJUSDT",
    "TIA":     "TIAUSDT",
    "ATOM":    "ATOMUSDT",
    "LTC":     "LTCUSDT",
    "BCH":     "BCHUSDT",
    "FIL":     "FILUSDT",
    "TRX":     "TRXUSDT",
    "ETC":     "ETCUSDT",
    "HBAR":    "HBARUSDT",
    "ICP":     "ICPUSDT",
    "AAVE":    "AAVEUSDT",
    "MKR":     "MKRUSDT",
    "STX":     "STXUSDT",
    "RUNE":    "RUNEUSDT",
    "SEI":     "SEIUSDT",
    "TON":     "TONUSDT",
    "ORDI":    "ORDIUSDT",
    "WLD":     "WLDUSDT",
    "JTO":     "JTOUSDT",
    "PYTH":    "PYTHUSDT",
    "TAO":     "TAOUSDT",
    "STRK":    "STRKUSDT",
    "BLUR":    "BLURUSDT",

    # Meme & low-priced
    "PEPE":    "PEPEUSDT",
    "WIF":     "WIFUSDT",
    "BONK":    "BONKUSDT",
    "SHIB":    "SHIBUSDT",
    "FLOKI":   "FLOKIUSDT",
    "POPCAT":  "POPCATUSDT",
    "MEW":     "MEWUSDT",
    "BOME":    "BOMEUSDT",
    "MEME":    "MEMEUSDT",
    "TURBO":   "TURBOUSDT",
    "DOG":     "DOGUSDT",
    "BRETT":   "BRETTUSDT",
    "MOG":     "MOGUSDT",
    "GOAT":    "GOATUSDT",
    "MOODENG": "MOODENGUSDT",
    "PNUT":    "PNUTUSDT",
    "ACT":     "ACTUSDT",

    # AI / Infra
    "RENDER":  "RENDERUSDT",
    "FET":     "FETUSDT",
    "JUP":     "JUPUSDT",
    "HYPE":    "HYPEUSDT",   # cek availability — Hype baru, mungkin belum di Bitget
    "ENA":     "ENAUSDT",
    "ONDO":    "ONDOUSDT",
    "ZK":      "ZKUSDT",
    "EIGEN":   "EIGENUSDT",
    "ETHFI":   "ETHFIUSDT",
    "PENDLE":  "PENDLEUSDT",
    "AI":      "AIUSDT",
    "PRIME":   "PRIMEUSDT",

    # SCALPER_ASSETS dari config.py
    "ZEC":     "ZECUSDT",
    "COMP":    "COMPUSDT",

    # "k" prefix — HL contract = 1000× underlying token
    # Bitget pakai contract 1 token per kontrak, jadi:
    #   contracts_bitget = contracts_hl × 1000
    # SymbolRegistry.get_contract_multiplier() return 1000 untuk kategori ini.
    "kPEPE":   "PEPEUSDT",
    "kBONK":   "BONKUSDT",
    "kSHIB":   "SHIBUSDT",
    "kFLOKI":  "FLOKIUSDT",
    "kDOGS":   "DOGSUSDT",
    "kNEIRO":  "NEIROUSDT",

    # HL-exclusive — pasti tidak ada di Bitget
    "FARTCOIN": None,
    "VINE":     None,
    "VVV":      None,
    "MON":      None,
    "REZ":      None,
    "SPX":      None,
    "HPOS":     None,
    "JELLY":    None,
    "PURR":     None,
    "@107":     None,    # HL index/synthetic
}


# Untuk asset yang HL pakai k-prefix tapi Bitget pakai full coin.
# Contract size HL per-1000 token, Bitget per-1 token.
# Multiplier: berapa "Bitget contracts" = 1 "HL contract".
K_PREFIX_MULTIPLIER = 1000.0


class SymbolRegistry:
    """
    Registry mapping HL → Bitget symbols dengan auto-discovery.

    Usage:
        registry = SymbolRegistry(bitget_client)
        await registry.initialize()
        bitget_sym = registry.get_bitget_symbol("BTC")  # "BTCUSDT"
        mult = registry.get_contract_multiplier("kPEPE") # 1000.0
    """

    def __init__(self, bitget_client=None):
        self.bitget = bitget_client
        self._hl_to_bitget: Dict[str, str] = {}
        self._bitget_to_hl: Dict[str, str] = {}
        self._available_at_both: Set[str] = set()
        self._initialized = False
        # Per-symbol contract info: symbol → {min_qty, qty_step, price_step, max_leverage}
        self._symbol_info: Dict[str, Dict] = {}

    async def initialize(self) -> int:
        """
        Validasi semua mapping vs daftar contract Bitget yang aktif.
        Asset yang tidak ada di Bitget akan dibuang dari mapping.

        Return: jumlah asset yang available di Bitget.
        """
        if self._initialized:
            return len(self._available_at_both)

        # Tanpa Bitget client: hanya pakai static mapping (mode degraded).
        if self.bitget is None:
            for hl_sym, bitget_sym in HL_TO_BITGET.items():
                if bitget_sym:
                    self._hl_to_bitget[hl_sym] = bitget_sym
                    self._bitget_to_hl[bitget_sym] = hl_sym
                    self._available_at_both.add(hl_sym)
            log.warning(
                f"[REGISTRY] Bitget client tidak tersedia — pakai static mapping "
                f"({len(self._available_at_both)} asset, tanpa validasi)."
            )
            self._initialized = True
            return len(self._available_at_both)

        try:
            contracts = await self.bitget.get_all_contracts()
        except Exception as e:
            log.error(f"[REGISTRY] Failed to fetch Bitget contracts: {e}")
            contracts = []

        # Build set of available Bitget symbols + extract per-symbol info.
        available_bitget: Set[str] = set()
        for c in contracts:
            sym = c.get("symbol", "")
            if not sym:
                continue
            available_bitget.add(sym)
            try:
                self._symbol_info[sym] = {
                    "min_qty":     float(c.get("minTradeNum") or c.get("minTradeAmount") or 0),
                    "qty_step":    float(c.get("sizeMultiplier") or c.get("volumePlace") or 0),
                    "price_step":  float(c.get("priceEndStep") or c.get("pricePlace") or 0),
                    "max_leverage": int(float(c.get("maxLever") or c.get("maxLeverage") or 100)),
                    "vol_place":   int(c.get("volumePlace", 0) or 0),
                    "price_place": int(c.get("pricePlace", 0) or 0),
                }
            except (ValueError, TypeError):
                self._symbol_info[sym] = {"max_leverage": 100}

        skipped = []
        for hl_sym, bitget_sym in HL_TO_BITGET.items():
            if bitget_sym is None:
                skipped.append(hl_sym)
                continue
            if bitget_sym in available_bitget:
                self._hl_to_bitget[hl_sym] = bitget_sym
                self._bitget_to_hl[bitget_sym] = hl_sym
                self._available_at_both.add(hl_sym)
            else:
                skipped.append(hl_sym)
                log.debug(f"[REGISTRY] {hl_sym} → {bitget_sym}: NOT on Bitget")

        log.warning(
            f"[REGISTRY] {len(self._available_at_both)}/{len(HL_TO_BITGET)} "
            f"HL assets available di Bitget"
        )
        if skipped:
            log.info(f"[REGISTRY] Skipped (HL-only / not in Bitget): {', '.join(skipped[:20])}{'...' if len(skipped) > 20 else ''}")

        self._initialized = True
        return len(self._available_at_both)

    def get_bitget_symbol(self, hl_asset: str) -> Optional[str]:
        return self._hl_to_bitget.get(hl_asset)

    def get_hl_asset(self, bitget_symbol: str) -> Optional[str]:
        return self._bitget_to_hl.get(bitget_symbol)

    def is_available(self, hl_asset: str) -> bool:
        return hl_asset in self._available_at_both

    @property
    def available_assets(self) -> Set[str]:
        return self._available_at_both.copy()

    def get_contract_multiplier(self, hl_asset: str) -> float:
        """
        Multiplier untuk konversi HL contracts → Bitget contracts.

        HL "k" prefix (kPEPE, kBONK) artinya 1 HL contract = 1000 token.
        Bitget pakai 1 contract = 1 token (untuk PEPE/BONK), jadi:
            bitget_contracts = hl_contracts × 1000
        Untuk non-k assets multiplier = 1.0.
        """
        if hl_asset.startswith("k") and len(hl_asset) > 1 and hl_asset[1].isupper():
            return K_PREFIX_MULTIPLIER
        return 1.0

    def get_max_leverage(self, hl_asset: str) -> int:
        """Max leverage Bitget allow untuk asset ini. Default 100."""
        bitget_sym = self.get_bitget_symbol(hl_asset)
        if not bitget_sym:
            return 100
        return self._symbol_info.get(bitget_sym, {}).get("max_leverage", 100)

    def get_symbol_info(self, hl_asset: str) -> Dict:
        bitget_sym = self.get_bitget_symbol(hl_asset)
        if not bitget_sym:
            return {}
        return self._symbol_info.get(bitget_sym, {})

    def filter_assets_for_scanning(self, hl_assets) -> list:
        """Filter HL asset list keep only yang available di Bitget."""
        if not self._initialized or not self._available_at_both:
            # Belum init — pass-through (jangan filter)
            return list(hl_assets)
        return [a for a in hl_assets if a in self._available_at_both]


# Singleton — di-init oleh main.py setelah BitgetClient connect
_registry: Optional[SymbolRegistry] = None


def get_registry() -> Optional[SymbolRegistry]:
    return _registry


def set_registry(reg: SymbolRegistry) -> None:
    global _registry
    _registry = reg
