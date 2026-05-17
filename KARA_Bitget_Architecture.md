# KARA Bot — Arsitektur Hyperliquid Data + Bitget Execution

**Dokumen Teknis — Bukan Sales Pitch**
**Tanggal:** 2026-05-14
**Versi KARA yang dianalisis:** 8.0.1
**Author:** AI Architecture Review

---

## BAGIAN 1: ANALISIS ARSITEKTUR YANG ADA

### Q1: Seberapa Bersih Pemisahan Data Layer dan Execution Layer?

**Jawaban singkat: Pemisahan TIDAK bersih. Ada tight coupling di beberapa titik kritis.**

#### Penilaian per komponen:

**ScoringEngine (`engine/scoring_engine.py`)**
- ✅ **Tidak** langsung import HyperliquidClient untuk scoring logic
- ❌ **Tapi** menerima `hl_client: HyperliquidClient` di `__init__` (bukan abstract interface)
- ❌ Memanggil `self.client.get_mark_price()`, `get_funding_data()`, `get_oi_data()`, 
  `get_candles()`, `_call_info_endpoint()`, `get_all_mids()` — semua HL-specific method
- ❌ `_run_scalper()` memanggil `self.client._call_info_endpoint("candleSnapshot", ...)` 
  — langsung ke HL internal endpoint, BUKAN abstract interface
- **Verdict: Scoring engine tight-coupled ke HyperliquidClient**

**RiskManager (`risk/risk_manager.py`)**
- ✅ **Bersih.** Import hanya dari `config`, `models.schemas`, `core.db`, `utils`
- ✅ Tidak ada dependency ke exchange apapun
- ✅ Bekerja dengan data abstrak (`AccountState`, `Position`, `TradeSignal`)
- **Verdict: Risk manager sepenuhnya exchange-agnostic — TIDAK PERLU DIUBAH**

**LiveExecutor (`execution/live_executor.py`)**
- ❌ Import eksplisit `HyperliquidClient`: `from data.hyperliquid_client import HyperliquidClient`
- ❌ `self.client = hl_client` — terikat ke HyperliquidClient
- ❌ `load_from_chain()` parse struktur response HL spesifik: `assetPositions`, `szi`, `entryPx`
- ❌ `get_account_state()` parse `marginSummary`, `accountValue`, `withdrawable`, `totalMarginUsed`
- ❌ `_place_onchain_sl()` memanggil `self.client.place_sl_order()` — HL-specific
- ❌ Order execution menggunakan `post_only` order type (HL specific)
- **Verdict: LiveExecutor 100% HL-specific, harus diganti bukan di-extend**

**PaperExecutor (`execution/paper_executor.py`)**
- ✅ **Bersih.** Tidak ada dependency ke exchange
- ✅ Hanya import `config`, `models.schemas`, `risk.risk_manager`, `utils`
- ✅ Menggunakan WS market cache untuk slippage simulation (exchange-agnostic)
- **Verdict: PaperExecutor bisa dipakai as-is untuk Bitget paper mode**

**main.py**
- ❌ Hardcode `HyperliquidClient` di `__init__`: `self.hl_client = HyperliquidClient()`
- ❌ `_update_positions()` memanggil `self.hl_client.get_mark_price_fast()` dan 
  `self.hl_client.get_candles()` untuk SEMUA open positions
- ❌ Ini artinya posisi Bitget akan di-monitor menggunakan harga dari Hyperliquid
- **Verdict: main.py perlu dimodifikasi, tapi modifikasi terbatas**

---

### Q2: Interface PaperExecutor dan LiveExecutor

**Jawaban: Keduanya TIDAK implement interface/base class yang sama. Ini problem arsitektur.**

#### Method yang ada di kedua executor:

| Method | PaperExecutor | LiveExecutor | Keterangan |
|--------|--------------|--------------|------------|
| `get_account_state()` | ✅ async | ✅ async | Return `AccountState` |
| `open_position(signal)` | ✅ async | ✅ async | Return `Optional[Position]` |
| `update_positions(prices)` | ✅ async | ✅ async | Return `List[Dict]` actions |
| `close_position(pos_id, price)` | ✅ async | ✅ async | Return `Optional[Position]` |
| `close_partial(pos_id, ratio, price)` | ✅ async | ✅ async | Partial TP |
| `load_from_db(chat_id)` | ✅ sync | ❌ tidak ada | Paper-only |
| `load_from_chain()` | ❌ tidak ada | ✅ async | Live-only |
| `open_positions` | ✅ property | ✅ property | List[Position] |

#### Method yang Hyperliquid-specific di LiveExecutor:
- `_place_onchain_sl(pos)` — pasang SL di exchange HL
- `_cancel_onchain_sl(pos_id)` — cancel SL order di HL
- `load_from_chain()` — parse struktur response HL
- Internal calls ke `self.client.set_leverage()`, `self.client.place_sl_order()`

**Kesimpulan:** Untuk tambah BitgetExecutor, perlu **abstract base class** `BaseExecutor` yang 
mendefinisikan interface kontrak, lalu PaperExecutor + LiveExecutor + BitgetExecutor 
masing-masing implement interface itu.

---

### Q3: Data Hyperliquid yang Digunakan untuk Scoring

Berikut **daftar lengkap** setiap data point yang diambil dari HL untuk signal generation:

#### Data REST API (via `_call_info_endpoint` atau SDK):

| Data Point | Method | Digunakan Untuk |
|-----------|--------|----------------|
| Mark price | `get_mark_price(asset)` | Entry price, SL/TP calculation, semua scoring |
| 1m candles (30 candle) | `_call_info_endpoint("candleSnapshot")` | EMA8/21, RSI14, CVD, volume surge, ATR |
| 15m candles | `get_candles(asset, "15m", limit=32)` | MTF trend filter |
| Funding rate | `get_funding_data(asset)` | OI+Funding analyzer (max 25 pts) |
| OI (open interest) | `get_oi_data(asset)` | OI change analyzer, squeeze detection |
| All mids (spot prices) | `get_all_mids()` | Basis/spot-perp premium calculation |
| All market metadata | `get_all_market_data()` | Max leverage per asset, mark price batch |
| Top volume markets | `get_top_volume_markets()` | Market selection filter |

#### Data WebSocket (via `ws_client.py`):

| Data Point | WS Channel | Digunakan Untuk |
|-----------|-----------|----------------|
| Orderbook (bids/asks) | `l2Book` | Bid/ask imbalance, VWAP deviation (max 25 pts) |
| Recent trades | `trades` | CVD (Cumulative Volume Delta), volume analysis |
| Funding rate live | `activeAssetCtx` | Real-time funding rate |
| Liquidation events | `liquidations` | Cascade risk scoring (max 25 pts) |

#### Data yang diturunkan (computed):
- 5-min OI delta (dari snapshot cache internal)
- Price change 1h (dari price history internal)
- MTF trend (bull/bear dari 15m EMA)
- Live candle dari WS trades (patch untuk candle terkini)
- Volatility regime (dari candle historical: TRENDING/RANGING/VOLATILE)
- Spot-perp basis (dari `all_mids` untuk `@ASSET` prefix)

**Total: ~9 data source aktif dari Hyperliquid + derived metrics**

---

### Q4: Data Hyperliquid yang Digunakan untuk Execution

#### Data HL yang dipakai saat eksekusi order:

| Data Point | Dipakai Di | Dampak Jika Di Bitget |
|-----------|-----------|----------------------|
| `signal.entry_price` | `open_position()` — limit order price | **KRITIS**: Ini harga HL, bukan harga Bitget |
| `signal.stop_loss` | `_place_onchain_sl()` | Dihitung dari harga HL |
| `signal.tp1/tp2/tp3` | `update_positions()` check | Dihitung dari harga HL |
| Max leverage dari metadata | `calculate_position_size()` | HL leverage tiers berbeda Bitget |
| Mark price (posisi terbuka) | `get_mark_price_fast()` di monitor loop | Harga HL dipakai untuk hitung unrealized PnL Bitget position |
| 1m & 15m candles | `_update_positions()` | Candle HL dipakai untuk momentum exit Bitget position |

**Temuan kritis:** `main.py:_update_positions()` memanggil `self.hl_client.get_mark_price_fast(asset)` 
untuk SEMUA open positions. Jika posisi ada di Bitget, harga yang digunakan untuk SL/TP check 
adalah harga Hyperliquid, **bukan** harga Bitget. Ini bisa menyebabkan SL/TP tidak akurat.

---

## BAGIAN 2: DESAIN ARSITEKTUR BARU

### Diagram Arsitektur Lengkap

```
════════════════════════════════════════════════════════════════════════════════
                         KARA BOT — ARSITEKTUR BARU
                    Hyperliquid Data  +  Bitget Execution
════════════════════════════════════════════════════════════════════════════════

┌─────────────────────────────────────────────────────────────────────────────┐
│                          DATA LAYER (TIDAK BERUBAH)                         │
│                                                                             │
│  ┌────────────────────┐    ┌────────────────────┐    ┌──────────────────┐  │
│  │  HyperliquidClient │    │  KaraWebSocketClient│    │  MarketDataCache │  │
│  │  (REST API)        │    │  (WS Streaming)     │    │  (In-Memory)     │  │
│  │                    │    │                     │    │                  │  │
│  │ • Mark prices      │    │ • Orderbook stream  │    │ • Orderbook      │  │
│  │ • Candles 1m/15m   │    │ • Trade stream      │    │ • Trades         │  │
│  │ • Funding rates    │    │ • Funding stream    │    │ • Funding        │  │
│  │ • OI data          │    │ • Liquidations      │    │ • Liquidations   │  │
│  │ • Market metadata  │    │                     │    │                  │  │
│  └────────────────────┘    └────────────────────┘    └──────────────────┘  │
│            │                         │                         │            │
│            └─────────────────────────┴─────────────────────────┘           │
│                                      │                                      │
│                              DATA FLOWS INTO                                │
└──────────────────────────────────────┼──────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         SIGNAL LAYER (TIDAK BERUBAH)                        │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                         ScoringEngine                               │   │
│  │                                                                     │   │
│  │  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐  │   │
│  │  │ OIFundingAnalyzer│  │  LiqAnalyzer     │  │  OBAnalyzer      │  │   │
│  │  │ (max 25 pts)     │  │  (max 25 pts)    │  │  (max 25 pts)    │  │   │
│  │  └──────────────────┘  └──────────────────┘  └──────────────────┘  │   │
│  │                                                                     │   │
│  │  + Session Bias (±5 to +15) + Regime Multiplier (×0.8-1.2)        │   │
│  │  + MTF Trend Filter + EMA8/21 + RSI + CVD + Volume Surge          │   │
│  │                                                                     │   │
│  │  OUTPUT: TradeSignal { asset, side, score, entry_price(HL),        │   │
│  │                        stop_loss(HL), tp1(HL), tp2(HL) }           │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────┼──────────────────────────────────────┘
                                       │
                                       │  TradeSignal (dengan harga HL)
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                     BRIDGE LAYER (KOMPONEN BARU)                            │
│                                                                             │
│  ┌────────────────────┐    ┌────────────────────┐    ┌──────────────────┐  │
│  │   SymbolRegistry   │    │    PriceBridge     │    │  (Future)        │  │
│  │   (NEW)            │    │    (NEW)           │    │  BitgetDataFeed  │  │
│  │                    │    │                    │    │  (WebSocket)     │  │
│  │ "BTC" → "BTCUSDT"  │    │ 1. Fetch Bitget Px │    │                  │  │
│  │ "kPEPE" → "PEPE"   │    │ 2. Adjust SL/TP    │    │                  │  │
│  │ "VVV" → None (skip)│    │ 3. Validate gap    │    │                  │  │
│  └────────────────────┘    └────────────────────┘    └──────────────────┘  │
└──────────────────────────────────────┼──────────────────────────────────────┘
                                       │
                                       │  TradeSignal (harga sudah di-adjust ke Bitget)
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      RISK LAYER (TIDAK BERUBAH)                             │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                        RiskManager                                  │   │
│  │  pre_trade_check() → calculate_position_size() → update_positions() │   │
│  │  Exchange-agnostic: hanya bekerja dengan AccountState + Position    │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────┼──────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                     EXECUTION LAYER (KOMPONEN BARU)                         │
│                                                                             │
│  ┌────────────────────┐    ┌────────────────────┐    ┌──────────────────┐  │
│  │  BaseExecutor      │    │   BitgetClient     │    │  BitgetExecutor  │  │
│  │  (abstract class)  │    │   (NEW)            │    │  (NEW)           │  │
│  │  NEW               │    │                    │    │                  │  │
│  │  • open_position() │    │ • place_order()    │    │ implement        │  │
│  │  • close_position()│    │ • get_positions()  │    │ BaseExecutor     │  │
│  │  • update_pos()    │    │ • get_balance()    │    │ interface        │  │
│  │  • get_account()   │    │ • set_leverage()   │    │                  │  │
│  └────────────────────┘    │ • get_mark_price() │    └──────────────────┘  │
│            ▲               └────────────────────┘                          │
│            │                         ▲                                     │
│  ┌─────────┴────────┐                │                                     │
│  │  PaperExecutor   │                │                                     │
│  │  (TIDAK BERUBAH) │                │                                     │
│  │  LiveExecutor    │────────────────┘ (Legacy, tetap ada untuk HL live)   │
│  │  (TIDAK BERUBAH) │                                                      │
│  └──────────────────┘                                                      │
└─────────────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      POSITION MONITOR (PERLU MODIFIKASI)                    │
│                                                                             │
│  main.py: _update_positions()                                               │
│                                                                             │
│  SEKARANG:  harga dari HL → cek SL/TP semua posisi                          │
│  BARU:      harga dari BITGET → cek SL/TP posisi Bitget                     │
│             harga dari HL → tetap untuk posisi HL (jika ada)               │
│                                                                             │
│  Candles untuk momentum exit juga harus dari Bitget, bukan HL              │
└─────────────────────────────────────────────────────────────────────────────┘

════════════════════════════════════════════════════════════════════════════════
                            ALIRAN DATA LENGKAP
════════════════════════════════════════════════════════════════════════════════

  HYPERLIQUID                          BITGET
  (Data Source)                        (Execution Target)
  ───────────                          ──────────────────
  WS: OB/Trades/Funding/Liq            REST: price, balance, positions
       │                                     │
       ▼                                     │
  ScoringEngine                             │
  (score + signal)                          │
       │                                     │
       ▼                                     │
  PriceBridge ◄───────────────────────────── │
  (fetch Bitget px, adjust SL/TP)           │
       │                                     │
       ▼                                     │
  RiskManager                               │
  (position sizing)                         │
       │                                     │
       ▼                                     │
  BitgetExecutor ──────────────────────────► │
  (place order)                              │
                                             │
  Position Monitor: Bitget price ◄─────────────────────────
  (SL/TP check uses Bitget mark price)
```

---

## BAGIAN 3: KOMPONEN YANG PERLU DIBUAT

### 3A: BitgetClient (`data/bitget_client.py`)

#### Library yang direkomendasikan:
- **Opsi 1: `pybitget` (Official SDK)** — `pip install pybitget`
  - Pro: Official, maintained, handle signature otomatis
  - Con: Async support terbatas, perlu wrapper
- **Opsi 2: REST langsung dengan `httpx`** — sesuai pattern existing code
  - Pro: Kontrol penuh, konsisten dengan `hyperliquid_client.py`
  - Con: Perlu implement signature HMAC-SHA256 sendiri

**Rekomendasi: httpx REST langsung** (konsisten dengan arsitektur existing)

#### Authentication Flow Bitget:
```
Header yang dibutuhkan:
  ACCESS-KEY:        API Key
  ACCESS-SIGN:       base64(HMAC-SHA256(timestamp + method + requestPath + body))
  ACCESS-TIMESTAMP:  Unix timestamp dalam milisecond
  ACCESS-PASSPHRASE: Passphrase saat buat API key
  Content-Type:      application/json
```

#### Method yang wajib ada:

```python
class BitgetClient:
    # ── Authentication ──────────────────────────
    def _sign(self, timestamp, method, path, body="") -> str: ...
    def _headers(self, method, path, body="") -> dict: ...

    # ── Market Data (untuk Price Bridge) ────────
    async def get_mark_price(self, symbol: str) -> float: ...
    # symbol format: "BTCUSDT" (bukan "BTC")

    async def get_ticker(self, symbol: str) -> dict: ...
    # Return: last, bid, ask, fundingRate, openInterest

    # ── Account ──────────────────────────────────
    async def get_account_balance(self) -> dict: ...
    # Return: available USDT, frozen USDT, total equity

    async def get_open_positions(self) -> List[dict]: ...
    # Return: list posisi dengan symbol, size, side, avgPx, uPnL

    # ── Order Management ─────────────────────────
    async def place_order(
        self,
        symbol: str,       # "BTCUSDT"
        side: str,         # "open_long" / "open_short" (hedge mode)
        order_type: str,   # "limit" / "market"
        size: float,       # contracts
        price: float = 0,  # 0 for market
        reduce_only: bool = False,
    ) -> dict: ...

    async def cancel_order(self, symbol: str, order_id: str) -> bool: ...

    async def get_open_orders(self, symbol: str = None) -> List[dict]: ...

    # ── Position Control ─────────────────────────
    async def set_leverage(self, symbol: str, leverage: int, hold_side: str = "long") -> bool: ...
    # hold_side: "long" atau "short" (Bitget butuh specify untuk hedge mode)

    async def set_margin_mode(self, symbol: str, mode: str = "isolated") -> bool: ...
    # mode: "crossed" atau "isolated"

    async def get_max_leverage(self, symbol: str) -> dict: ...
    # Return: max_leverage per tier berdasarkan position size
```

#### Rate Limit Bitget vs Hyperliquid:

| Metric | Hyperliquid | Bitget |
|--------|-------------|--------|
| Public data | ~10 req/s | 20 req/s |
| Private (order) | ~10 req/s | 10 req/s |
| WebSocket | Unlimited stream | Unlimited stream |
| Burst limit | ~30 req/10s | 60 req/min |

Bitget lebih permisif untuk public data, tapi order endpoint lebih konservatif.

#### Error Handling Bitget (berbeda dari HL):
- Bitget menggunakan HTTP 200 untuk semua response, error ada di body
- Pattern: `{"code": "00000", "msg": "success", "data": {...}}`
- Error code `"40007"` = Invalid API key, `"40013"` = Insufficient balance
- Berbeda dari HL yang pakai HTTP status code (502, 429, dll)

---

### 3B: BitgetExecutor (`execution/bitget_executor.py`)

#### Interface Base Class (harus dibuat dulu):

```python
# execution/base_executor.py — BARU
from abc import ABC, abstractmethod

class BaseExecutor(ABC):
    @abstractmethod
    async def get_account_state(self) -> AccountState: ...

    @abstractmethod
    async def open_position(self, signal: TradeSignal) -> Optional[Position]: ...

    @abstractmethod
    async def close_position(self, position_id: str, current_price: float) -> Optional[Position]: ...

    @abstractmethod
    async def close_partial(self, position_id: str, close_ratio: float, price: float) -> Optional[Position]: ...

    @abstractmethod
    async def update_positions(self, prices: Dict[str, float]) -> List[Dict]: ...

    @property
    @abstractmethod
    def open_positions(self) -> List[Position]: ...
```

**PaperExecutor dan LiveExecutor** perlu ditambahkan `(BaseExecutor)` sebagai parent class 
tanpa mengubah logic internal — hanya tambahkan inheritance.

#### Cara agar RiskManager tidak perlu diubah:

RiskManager sudah exchange-agnostic. Dia hanya butuh:
1. `AccountState` object — bisa disupply oleh `BitgetExecutor.get_account_state()`
2. `TradeSignal` object — signal dari ScoringEngine (tidak berubah)
3. `List[Position]` — bisa disupply oleh `BitgetExecutor.open_positions`

**RiskManager TIDAK PERLU DIUBAH sama sekali.**

#### Cara agar main.py minimal perubahannya:

Tambahkan config switch di `config.py`:
```python
EXECUTION_EXCHANGE = os.getenv("KARA_EXECUTION_EXCHANGE", "hyperliquid")  # "hyperliquid" | "bitget"
```

Di `core/user_session.py` (bukan main.py), ubah executor factory:
```python
if EXECUTION_EXCHANGE == "bitget" and user.config.bot_mode == BotMode.LIVE:
    executor = BitgetExecutor(chat_id, bitget_client, risk_manager)
elif user.config.bot_mode == BotMode.LIVE:
    executor = LiveExecutor(chat_id, hl_client, risk_manager)
else:
    executor = PaperExecutor(risk_manager, initial_balance, chat_id)
```

Perubahan di main.py hanya 2 baris: inisiasi `bitget_client` dan pass ke user session factory.

#### Hal penting di BitgetExecutor:

**1. Hedge Mode vs One-Way Mode:**

Bitget default menggunakan Hedge Mode (ada posisi LONG dan SHORT terpisah).
Ini berbeda dari HL yang One-Way.

```python
# Bitget hedge mode side mapping:
# Signal LONG  → order side = "open_long"  (bukan "buy")
# Signal SHORT → order side = "open_short" (bukan "sell")
# Close LONG   → order side = "close_long"
# Close SHORT  → order side = "close_short"
```

**2. Symbol Format (lihat 3D):**
Mapping wajib sebelum setiap API call ke Bitget.

**3. Minimum Order Size Bitget:**
- BTC: min 0.001 contracts (~$78 notional)
- ETH: min 0.01 contracts (~$2.5 notional)
- Altcoin: varies, min 1-10 contracts

Untuk KARA dengan modal $62.50, ini KRITIS — banyak altcoin mungkin tidak bisa diorder 
karena minimum size Bitget terlalu besar untuk posisi student.

**4. Isolated Margin Setup:**
Bitget perlu set margin mode DAN leverage SEBELUM open position:
```python
await bitget_client.set_margin_mode(symbol, "isolated")
await bitget_client.set_leverage(symbol, leverage, hold_side)
```

---

### 3C: Price Bridge (`utils/price_bridge.py`)

```python
class PriceBridge:
    def __init__(self, bitget_client: BitgetClient, symbol_registry: SymbolRegistry):
        self.bitget = bitget_client
        self.registry = symbol_registry
        self._price_cache: Dict[str, Tuple[float, float]] = {}  # symbol -> (price, timestamp)
        self._cache_ttl = 2.0  # 2 detik — cukup untuk burst signal, tidak stale

    async def adjust_signal_to_bitget(
        self,
        signal: TradeSignal,
        max_price_gap_pct: float = 0.003  # 0.3% max gap HL vs Bitget
    ) -> Optional[TradeSignal]:
        """
        Sebelum eksekusi: ambil harga Bitget, recalculate SL/TP.
        Return None jika gap terlalu besar (skip trade).
        """
        bitget_symbol = self.registry.get_bitget_symbol(signal.asset)
        if not bitget_symbol:
            return None  # asset tidak ada di Bitget

        # Fetch Bitget mark price
        bitget_price = await self.bitget.get_mark_price(bitget_symbol)
        if bitget_price <= 0:
            return None

        # Check price gap
        hl_price = signal.entry_price
        gap_pct = abs(bitget_price - hl_price) / hl_price

        if gap_pct > max_price_gap_pct:
            log.warning(
                f"[BRIDGE] {signal.asset}: HL={hl_price:.4f} Bitget={bitget_price:.4f} "
                f"gap={gap_pct*100:.3f}% > {max_price_gap_pct*100:.1f}% — skip trade"
            )
            return None

        # Recalculate levels based on Bitget price
        # Pertahankan PERSENTASE yang sama, tapi hitung ulang dari harga Bitget
        is_long = signal.side == Side.LONG

        sl_pct  = abs(signal.stop_loss - hl_price) / hl_price
        tp1_pct = abs(signal.tp1 - hl_price) / hl_price
        tp2_pct = abs(signal.tp2 - hl_price) / hl_price
        tp3_pct = abs(signal.tp3 - hl_price) / hl_price if signal.tp3 else 0

        adjusted = signal.model_copy(deep=True)
        adjusted.entry_price = bitget_price

        if is_long:
            adjusted.stop_loss = bitget_price * (1 - sl_pct)
            adjusted.tp1       = bitget_price * (1 + tp1_pct)
            adjusted.tp2       = bitget_price * (1 + tp2_pct)
            adjusted.tp3       = bitget_price * (1 + tp3_pct) if tp3_pct else 0
        else:
            adjusted.stop_loss = bitget_price * (1 + sl_pct)
            adjusted.tp1       = bitget_price * (1 - tp1_pct)
            adjusted.tp2       = bitget_price * (1 - tp2_pct)
            adjusted.tp3       = bitget_price * (1 - tp3_pct) if tp3_pct else 0

        return adjusted

    async def get_bitget_price(self, hl_asset: str) -> float:
        """Get current Bitget mark price for an HL asset name."""
        bitget_symbol = self.registry.get_bitget_symbol(hl_asset)
        if not bitget_symbol:
            return 0.0

        # Check cache first
        cached = self._price_cache.get(bitget_symbol)
        if cached and (time.time() - cached[1]) < self._cache_ttl:
            return cached[0]

        price = await self.bitget.get_mark_price(bitget_symbol)
        if price > 0:
            self._price_cache[bitget_symbol] = (price, time.time())
        return price
```

---

### 3D: Symbol Registry (`utils/symbol_registry.py`)

#### Pemetaan Symbol yang Harus Ada:

```python
# HL symbol → Bitget USDT-M perp symbol
HL_TO_BITGET: Dict[str, Optional[str]] = {
    # Major
    "BTC":       "BTCUSDT",
    "ETH":       "ETHUSDT",
    "SOL":       "SOLUSDT",
    "BNB":       "BNBUSDT",
    "XRP":       "XRPUSDT",
    "DOGE":      "DOGEUSDT",
    "ADA":       "ADAUSDT",
    "AVAX":      "AVAXUSDT",
    "LINK":      "LINKUSDT",
    "DOT":       "DOTUSDT",
    "UNI":       "UNIUSDT",
    "NEAR":      "NEARUSDT",
    "ARB":       "ARBUSDT",
    "OP":        "OPUSDT",
    "SUI":       "SUIUSDT",
    "APT":       "APTUSDT",
    "INJ":       "INJUSDT",
    "TIA":       "TIAUSDT",
    "PEPE":      "PEPEUSDT",
    "WIF":       "WIFUSDT",
    "RENDER":    "RENDERUSDT",
    "FET":       "FETUSDT",
    "JUP":       "JUPUSDT",
    "HYPE":      "HYPEUSDT",

    # HL pakai "k" prefix untuk token kecil (harga per 1000 token)
    # Bitget tidak pakai prefix ini
    "kPEPE":     "PEPEUSDT",    # HATI-HATI: size calculation berbeda!
    "kBONK":     "BONKUSDT",
    "kSHIB":     "SHIBUSDT",
    "kFLOKI":    "FLOKIUSDT",

    # SCALPER_ASSETS khusus dari config.py
    "ZEC":       "ZECUSDT",
    "SPX":       None,          # Kemungkinan tidak ada di Bitget (SPX perp jarang)
    "COMP":      "COMPUSDT",
    "REZ":       None,          # Kemungkinan tidak ada di Bitget
    "PYTH":      "PYTHUSDT",
    "MON":       None,          # Kemungkinan tidak ada di Bitget
    "VVV":       None,          # Tidak ada di Bitget

    # Berisiko tidak ada di Bitget
    "FARTCOIN":  None,          # Hanya ada di Hyperliquid
    "VINE":      None,          # Hanya ada di Hyperliquid
    "HPOS":      None,
}
```

**CATATAN KRITIS untuk "k" prefix:**
- Di HL, `kPEPE` artinya kontrak per 1000 PEPE (mark price ~$0.000014 × 1000 = ~$0.014)
- Di Bitget, `PEPEUSDT` adalah 1 kontrak per X PEPE (tergantung contract size)
- Size konversi TIDAK trivial — butuh perhatian khusus saat hitung jumlah kontrak

#### Auto-discovery saat startup:

```python
class SymbolRegistry:
    def __init__(self, bitget_client: BitgetClient):
        self.bitget = bitget_client
        self._hl_to_bitget = {}
        self._available_at_both: Set[str] = set()  # asset yang ada di KEDUANYA

    async def initialize(self):
        """Cek ketersediaan asset di Bitget, build available set."""
        bitget_symbols = await self.bitget.get_all_symbols()
        bitget_set = set(bitget_symbols)

        for hl_sym, bitget_sym in HL_TO_BITGET.items():
            if bitget_sym and bitget_sym in bitget_set:
                self._hl_to_bitget[hl_sym] = bitget_sym
                self._available_at_both.add(hl_sym)
            else:
                log.info(f"[REGISTRY] {hl_sym} → {bitget_sym}: NOT AVAILABLE on Bitget, skip")

        log.info(
            f"[REGISTRY] {len(self._available_at_both)}/{len(HL_TO_BITGET)} "
            f"HL assets available on Bitget"
        )

    def get_bitget_symbol(self, hl_asset: str) -> Optional[str]:
        return self._hl_to_bitget.get(hl_asset)

    def is_available(self, hl_asset: str) -> bool:
        return hl_asset in self._available_at_both

    @property
    def available_assets(self) -> Set[str]:
        return self._available_at_both.copy()
```

---

### 3E: Config Updates (`config.py`)

Tambahkan section baru setelah Hyperliquid credentials:

```python
# ──────────────────────────────────────────────
# BITGET CREDENTIALS (opsional — hanya untuk Bitget execution)
# ──────────────────────────────────────────────
BITGET_API_KEY    = os.getenv("BITGET_API_KEY", "").strip()
BITGET_SECRET_KEY = os.getenv("BITGET_SECRET_KEY", "").strip()
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "").strip()

# ── Execution Exchange ────────────────────────────────────────────────────────
# "hyperliquid" = live trade di HL (existing behavior)
# "bitget"      = live trade di Bitget, data tetap dari HL
EXECUTION_EXCHANGE = os.getenv("KARA_EXECUTION_EXCHANGE", "hyperliquid").lower()

# DATA_SOURCE tetap "hyperliquid" — tidak berubah
# DATA_SOURCE = os.getenv("KARA_DATA_SOURCE", "mainnet")

# ── Price Bridge Config ───────────────────────────────────────────────────────
PRICE_BRIDGE_MAX_GAP_PCT = float(os.getenv("KARA_PRICE_BRIDGE_MAX_GAP", "0.003"))  # 0.3%
PRICE_BRIDGE_CACHE_TTL_S = float(os.getenv("KARA_PRICE_BRIDGE_TTL", "2.0"))
```

---

## BAGIAN 4: MASALAH TEKNIS YANG HARUS DISELESAIKAN

### Masalah 1: Price Slippage Antar Exchange

**Apa masalahnya:**
Sinyal dibuat dari harga HL ($78,000). Saat eksekusi di Bitget, harga bisa berbeda 
($78,015). SL dan TP yang dihitung dari harga HL akan off dengan margin tersebut.

**Seberapa besar dampaknya:**
- Gap normal: 0.01-0.05% = $7-39 untuk BTC. Untuk scalper dengan SL 1.5%, ini **material** 
  karena bisa geser effective SL pct sebesar 3-10% dari nilai SL itu sendiri.
- Gap ekstrem (news/spike): bisa 0.2-0.5% = $156-390 untuk BTC
- Untuk altcoin kecil: gap bisa lebih besar karena likuiditas tipis

**Solusi yang direkomendasikan:**
1. `PriceBridge` wajib fetch harga Bitget sebelum eksekusi (sudah didesain di 3C)
2. Recalculate semua level dengan persentase yang sama dari harga Bitget (bukan offset)
3. Jika gap > 0.3%, skip trade dan log alasannya
4. Cache harga 2 detik untuk burst signals (cegah spam API)

**Jawaban untuk pertanyaan:**
- Apakah SL/TP harus di-recalculate? **YA, WAJIB.** Tanpa ini, SL bisa kena lebih awal atau 
  terlambat karena basis harga berbeda.
- Toleransi maksimum: **0.3%** untuk scalper (karena SL hanya 1.5%, gap 0.3% = 20% dari SL)
- Handle Bitget tiba-tiba jauh: skip trade, log peringatan, jangan force execute

---

### Masalah 2: Asset Availability

**Apa masalahnya:**
Dari 100 HL assets yang di-scan KARA, estimasi hanya 60-70 yang ada di Bitget.

**Assets yang pasti tidak ada di Bitget:**
- `FARTCOIN`, `VINE`, `VVV`, `MON`, `REZ`, `SPX` — HL-exclusive assets
- Beberapa microcap HL yang belum listed di exchange lain

**Dampak:**
- 30-40% sinyal dari market scanner akan di-skip saat eksekusi
- SCALPER_ASSETS seperti `VVV`, `MON`, `REZ` yang punya WR tinggi tidak bisa dieksekusi
- Miss opportunities yang justru high-value

**Solusi:**
1. `SymbolRegistry.initialize()` saat startup → build `available_at_both` set
2. Di `_scan_all_assets()`, filter: hanya scan assets yang ada di `registry.available_assets`
3. Log satu kali saat startup: "Skip scanning: VVV, MON, REZ, FARTCOIN (not on Bitget)"
4. Tidak perlu scan assets yang pasti tidak bisa dieksekusi

---

### Masalah 3: Symbol Format Berbeda

**Apa masalahnya:**
HL: `"BTC"`, `"kPEPE"`, `"kBONK"` 
Bitget: `"BTCUSDT"`, `"PEPEUSDT"`, `"BONKUSDT"`

**Yang paling kritis: "k" prefix dan implikasinya:**
- `kPEPE` di HL: 1 kontrak = 1000 PEPE (karena harga PEPE sangat kecil)
- Bitget PEPEUSDT: contract size berbeda, perlu cek di API Bitget
- Jika salah konversi: bisa order 1000x lebih besar atau kecil dari yang diinginkan

**Solusi:**
- `SymbolRegistry` harus juga menyimpan `contract_size_multiplier` per asset
- Saat hitung `contracts` di `calculate_position_size()`, terapkan multiplier
- Atau: untuk "k" prefix assets, disable dulu di Bitget mode sampai ada validasi manual

---

### Masalah 4: Leverage Tiers Bitget

**Apa masalahnya:**
Bitget punya tiered leverage. Contoh untuk BTCUSDT:
- Posisi < $10,000: max 125x
- Posisi $10,000-$50,000: max 100x
- Posisi > $500,000: max 20x

Untuk KARA dengan modal $62.50, leverage 15x seharusnya aman di tier terendah.
Tapi beberapa altcoin mungkin max leverage 5-10x saja.

**Dampak:** Medium. Untuk modal kecil biasanya tidak masalah, tapi perlu dicek per-asset.

**Solusi:**
1. `BitgetClient.get_max_leverage()` dipanggil saat set leverage
2. Cap leverage ke min(requested_leverage, bitget_max_leverage)
3. Log jika leverage di-reduce: `"BTC: requested 15x but Bitget max for this size = 10x"`
4. Jangan abort trade — kurangi leverage dan tetap eksekusi

---

### Masalah 5: Funding Rate Berbeda

**Apa masalahnya:**
Funding rate Bitget dan HL berbeda karena mekanisme berbeda.
- HL: interval 8 jam, dibayar setiap 8 jam
- Bitget: interval bisa 4 atau 8 jam tergantung kontrak

Signal KARA mungkin SHORT karena funding HL sangat positif (+0.05%).
Di Bitget, funding mungkin +0.02% — masih positif, sinyal tetap valid.
Tapi bisa juga funding Bitget -0.01% — dalam kondisi ini SHORT kurang menguntungkan dari sisi funding.

**Dampak:** Low-Medium. Funding bukan driver utama signal KARA — signal lebih ke orderbook + momentum.
Funding hanya berkontribusi maksimal 25 pts dari 100 pts total score.

**Rekomendasi:**
Untuk scalper dengan hold time 20-30 menit, satu periode funding (8 jam) tidak terkena.
**Cukup ignore perbedaan ini.** Tambahkan Bitget funding check hanya jika:
- Trade mode standard dengan hold > 8 jam
- Atau fundng rate HL sangat ekstrim (> 0.1% per 8h) sebagai signal utama

---

### Masalah 6: Latency Tambahan

**Sekarang (HL data + HL execution):**
```
Signal → HL order = 100-300ms total
```

**Dengan Bitget:**
```
Signal detected     : 0ms
PriceBridge fetch   : +100-200ms  (GET mark price Bitget)
Level recalculation : +1ms
BitgetClient order  : +200-500ms  (POST order Bitget)
Total               : 300-700ms
```

**Dampak untuk scalper 20 menit:** Medium. 
Untuk scalper hold 20 menit, 500ms latency tambahan = 0.04% dari hold time.
Pada harga BTC $78,000 dengan volatility 0.1%/menit, 500ms = $65 potential movement.
Ini tidak signifikan untuk entry price, tapi bisa material jika market sedang spike.

**Mitigasi:**

1. **Cache harga Bitget** — 2 detik TTL, jangan fetch ulang jika baru saja fetch
2. **Parallel fetch** — saat signal digenerate, langsung trigger background fetch harga Bitget
3. **Pre-warm** — setelah signal threshold terpenuhi (~scoring selesai), fetch Bitget price 
   sebelum risk check selesai (concurrent)
4. **WebSocket Bitget** — jangka panjang, subscribe WS Bitget untuk mark price real-time
   (eliminasi HTTP fetch latency, turunkan ke <50ms)

---

### Masalah 7: Position Sync

**Apa masalahnya:**
Jika bot crash saat ada posisi terbuka di Bitget:
- Bot tidak tahu posisi ada
- SL/TP tidak di-monitor
- Posisi bisa kena SL di exchange tapi bot tidak tahu

**Solusi yang harus ada:**

```python
class BitgetExecutor:
    async def load_from_exchange(self):
        """Startup sync: ambil semua open positions dari Bitget."""
        positions = await self.bitget_client.get_open_positions()
        for pos_data in positions:
            # Reconstruct Position object dari data exchange
            # SL/TP tidak diketahui — set fallback 3% SL
            pos = self._reconstruct_position(pos_data)
            self._positions[pos.position_id] = pos
            log.warning(f"[BITGET] Recovered position: {pos.asset} {pos.side.value}")
```

**Perbedaan dari LiveExecutor:**
- HL: posisi ada di chain, bisa di-query kapan saja
- Bitget: posisi ada di exchange API, query via REST
- Bitget tidak punya "on-chain SL" seperti HL — SL harus di-manage oleh bot

**Implikasi:** Jika bot mati, posisi Bitget tidak ada SL protection otomatis!
HL lebih aman karena ada on-chain SL yang aktif meski bot mati.

---

### Masalah 8: OI dan Liquidation Data

**Apa masalahnya:**
KARA menggunakan OI HL + liquidation events HL untuk scoring.
Apakah ini valid untuk prediksi harga di Bitget?

**Analisis korelasi:**
- BTC/ETH/SOL: Korelasi sangat tinggi (>0.95). Gerakan price di HL hampir identik dengan Bitget 
  karena arbitrageur menjaga spread tetap ketat. OI HL yang tinggi artinya ada crowding 
  yang akan dilikuidasi, dan likuidasi itu akan menekan harga di semua exchange.
- Altcoin mid-cap: Korelasi sedang (0.7-0.9). Volume lebih terpusat di satu exchange,
  jadi OI dan likuidasi di exchange lain tidak selalu relevan.
- Microcap/HL-exclusive: OI HL = OI global karena hanya ada di HL. Tidak relevan untuk Bitget.

**Rekomendasi:**
- Untuk BTC, ETH, SOL: **OI HL data cukup valid.** Tidak perlu tambah Bitget OI.
- Untuk altcoin: Sinyal HL masih berguna sebagai global market sentiment,
  tapi liquidation cascade di HL tidak selalu sama di Bitget.
- Untuk kasus production: pertimbangkan disable sinyal altcoin yang OI-driven 
  untuk Bitget execution mode.

---

## BAGIAN 5: IMPLEMENTATION ROADMAP

### Phase 1 — Foundation (tanpa risiko, bisa paralel dengan live sistem)
*Estimasi: 3-5 hari*

```
□ 1.1 Buat BaseExecutor abstract class
      File: execution/base_executor.py
      Effort: 2 jam
      Risk: Zero — tidak mengubah file apapun yang ada

□ 1.2 Tambahkan (BaseExecutor) ke PaperExecutor dan LiveExecutor
      File: execution/paper_executor.py, execution/live_executor.py
      Effort: 30 menit
      Risk: Minimal — hanya tambah inheritance, tidak ubah logic

□ 1.3 Buat SymbolRegistry dengan mapping lengkap
      File: utils/symbol_registry.py
      Effort: 4 jam
      Risk: Zero — file baru

□ 1.4 Buat BitgetClient (REST only, public endpoint dulu)
      File: data/bitget_client.py
      Effort: 1 hari
      Test: Unit test get_mark_price(), get_ticker() tanpa auth

□ 1.5 Tambahkan config variables baru ke config.py
      File: config.py
      Effort: 30 menit
      Risk: Minimal — hanya tambah, tidak ubah yang ada

□ 1.6 Buat PriceBridge
      File: utils/price_bridge.py
      Effort: 4 jam
      Test: Unit test dengan mock Bitget client
```

*Testing yang diperlukan: Unit tests per komponen, tidak perlu live API*

### Phase 2 — Integration (butuh testing intensif)
*Estimasi: 5-7 hari*

```
□ 2.1 BitgetClient — private endpoints (order placement)
      Effort: 1 hari
      Test: Bitget testnet (tersedia di api.bitget.com/api/mix/v1/...)
      Risk: Sedang — salah signature = order gagal (tidak rugi)

□ 2.2 BitgetExecutor
      File: execution/bitget_executor.py
      Effort: 2 hari
      Test: Paper trading mode dengan BitgetExecutor
             (simulasi posisi, tapi order dikirim ke Bitget testnet)
      Risk: Sedang — perlu validasi symbol mapping dan size calculation

□ 2.3 Modifikasi main.py — inject BitgetClient + position monitor
      File: main.py, core/user_session.py
      Effort: 4 jam
      Risk: Sedang — position monitor loop perlu pilih price source yang tepat

□ 2.4 Integrasi SymbolRegistry ke market scanner
      File: main.py (_scan_all_assets)
      Effort: 2 jam
      Test: Verifikasi hanya asset yang ada di Bitget yang masuk watchlist
      Risk: Low

□ 2.5 End-to-end paper test
      Setup: EXECUTION_EXCHANGE=bitget, KARA_TRADE_MODE=paper
      Duration: 1-2 hari observasi
      Target: 20+ paper trades routed ke Bitget testnet berhasil
```

*Testing yang diperlukan: Integration test dengan Bitget testnet, verifikasi symbol mapping*

### Phase 3 — Live Switch (production, hati-hati)
*Estimasi: 2-3 hari (termasuk monitoring)*

```
□ 3.1 Deploy ke staging dengan EXECUTION_EXCHANGE=bitget
      Verifikasi: Startup logs menunjukkan Bitget connected
      Verifikasi: Symbol registry initialized, available assets logged

□ 3.2 Live test dengan modal kecil ($10-20)
      Biarkan 5-10 trade pertama berjalan
      Monitor: slippage actual, latency, fill rate

□ 3.3 Verifikasi position sync setelah restart paksa
      Test: Kill bot saat ada open position, restart, verifikasi sync

□ 3.4 Full production switch
      Set EXECUTION_EXCHANGE=bitget
      Monitor 24 jam pertama secara ketat
```

*Risiko: Real money. Pastikan Phase 2 sudah 100% solid sebelum lanjut ke sini.*

---

## BAGIAN 6: KEKURANGAN DAN RISIKO ARSITEKTUR INI

### Tabel Risiko Lengkap

| Kekurangan | Dampak | Tingkat Risiko | Bisa Dimitigasi? |
|------------|--------|----------------|------------------|
| **TEKNIS** | | | |
| Price divergence HL vs Bitget | SL/TP meleset, bisa SL lebih awal/terlambat | TINGGI | Ya — PriceBridge wajib |
| Latency tambahan (300-700ms) | Entry price lebih buruk saat market cepat | SEDANG | Parsial — cache + WS |
| Symbol availability gap (30-40% skip) | Miss sinyal valid dari HL-exclusive assets | SEDANG | Tidak — by design |
| Funding rate mismatch HL vs Bitget | Signal kurang akurat untuk funding-driven trades | RENDAH | Ya — tambahkan Bitget FR check |
| "k" prefix size conversion error | Over/under size order secara signifikan | TINGGI | Ya — explicit mapping + test |
| Tidak ada on-chain SL di Bitget | Bot crash = posisi tanpa SL protection | TINGGI | Parsial — perlu SL order management |
| Bitget outage saat HL ok | Bot tidak bisa eksekusi, sinyal terbuang | SEDANG | Parsial — fallback ke paper, alert |
| HL outage saat Bitget ok | Bot tidak bisa generate sinyal | SEDANG | Tidak — HL tetap data source |
| Complexity dua klien | Bug lebih sulit di-debug | SEDANG | Parsial — logging yang baik |
| **STRATEGIS** | | | |
| Sinyal HL mungkin tidak akurat untuk Bitget price | Khususnya untuk altcoin midcap | SEDANG | Parsial — filter ke BTC/ETH/major |
| Liquidation cascade HL ≠ Bitget | Signal liq-driven kurang valid | SEDANG | Parsial — disable liq-driven signals |
| OI data HL tidak refleksi Bitget trader | Terutama altcoin yang volume Bitget > HL | SEDANG | Parsial — use only major assets |
| Funding rate signal invalid untuk Bitget hold | Funding HL dibayar ke HL trader, bukan Bitget | RENDAH | Ya — ignore funding dalam scoring untuk Bitget mode |
| **OPERASIONAL** | | | |
| Dua API key keamanan | Dua attack surface | SEDANG | Ya — enkripsi, rotate regularly |
| Debug lebih kompleks | Two-system tracing | SEDANG | Parsial — unified logging |
| Biaya fee Bitget vs HL | Bitget taker 0.06% vs HL maker 0.00% (rebate) | SEDANG | Parsial — gunakan limit order |
| Minimum order size Bitget | Modal $62.50 mungkin tidak cukup untuk beberapa altcoin | TINGGI | Ya — filter by min order size |
| Dua exchange independen bisa down | Double failure mode | SEDANG | Parsial — fallback ke paper |

---

## BAGIAN 7: REKOMENDASI AKHIR

### Apakah arsitektur ini worth it untuk diimplementasi?

**Jawaban jujur: TERGANTUNG KONDISI. Dalam sebagian besar kasus untuk user KARA saat ini, TIDAK worth it.**

---

### Skenario di mana HL data + Bitget execution MASUK AKAL:

1. **User sudah punya akun Bitget yang established** dengan KYC lengkap, deposit > $1000,
   dan TIDAK mau buka akun Hyperliquid karena berbagai alasan (jurisdiksi, preferensi, dll).

2. **User ingin cross-exchange signal** dengan Bitget sebagai secondary execution venue
   untuk diversifikasi — HL posisi penuh, overflow ke Bitget.

3. **Developer yang sedang build multi-exchange bot** dan ingin arsitektur yang bisa scale.

4. **Latency tidak kritis** — user sudah puas dengan scalper hold 20+ menit dan tidak masalah
   dengan tambahan 500ms latency.

5. **Asset yang diinginkan tersedia di Bitget** — portfolio terfokus ke BTC/ETH/SOL major coins
   yang ada di kedua exchange.

---

### Skenario di mana lebih baik tetap full Hyperliquid:

1. **User baru / student** — dua exchange = dua set credential + dua learning curve + 
   dua potensi error. Single-exchange lebih aman dan mudah.

2. **Modal kecil < $200** — Bitget minimum order size bisa menjadi bottleneck.
   HL lebih fleksibel untuk micro-sizing.

3. **SCALPER_ASSETS yang punya high WR** (`VVV`, `MON`, `REZ`, `FARTCOIN`) adalah HL-exclusive.
   Pindah ke Bitget berarti kehilangan sinyal-sinyal terbaik KARA.

4. **Butuh on-chain SL safety** — HL punya on-chain SL yang aktif meski bot crash.
   Bitget tidak punya equivalent yang sama. Untuk non-technical user, ini risiko besar.

5. **Fee structure lebih baik di HL** — Hyperliquid memberi maker rebate (bayaran untuk buat
   limit order). Bitget taker fee 0.06% = mahal untuk scalper yang butuh fill cepat.

6. **Latency scalping** — 20-menit scalper dengan SL 1.5% sudah tight. Tambahan 500ms latency 
   dari Bitget round-trip bisa berarti entry price lebih buruk secara konsisten.

---

### Alternatif yang Mungkin Lebih Baik:

**Alternatif 1: HL sebagai primary, Bitget sebagai backup**
- Jika HL API tidak available (502 backoff aktif > 5 menit), route execution ke Bitget
- Sinyal tetap dari HL, eksekusi di HL kalau bisa, fallback ke Bitget jika tidak bisa
- Lebih aman karena Bitget path jarang digunakan

**Alternatif 2: Mirror trading (HL → Bitget simultaneous)**
- Eksekusi di HL AND Bitget secara bersamaan dengan size yang dibagi
- Diversifikasi liquidity risk
- Kompleksitas double, tapi setiap exchange handle sebagian risk

**Alternatif 3: Bitget data + Bitget execution (standalone)**
- Jika user benar-benar ingin Bitget, lebih baik build adapter Bitget data layer juga
- Sehingga sinyal based on Bitget OB/Funding, bukan HL
- Lebih konsisten, tidak ada cross-exchange data mismatch
- Effort lebih besar tapi arsitektur lebih bersih

**Rekomendasi implementasi jika tetap mau jalan:**

Prioritas urutan implementasi untuk minimize risk dan maximize learning:
```
1. Implementasi SymbolRegistry dulu → tahu berapa % asset yang available
2. Jika < 60% asset available → pertimbangkan ulang
3. Kalau lanjut → BitgetClient dengan unit test lengkap
4. PriceBridge + validation logic gap
5. BitgetExecutor dengan paper mode dulu (1-2 minggu observasi)
6. Live switch dengan modal sangat kecil ($10-20) untuk validation
```

---

## APPENDIX: File Changes Summary

### File Baru (tidak mengubah yang ada):
```
data/bitget_client.py          — REST client Bitget
execution/base_executor.py     — Abstract interface
execution/bitget_executor.py   — Bitget execution implementation
utils/symbol_registry.py       — HL↔Bitget symbol mapping
utils/price_bridge.py          — Price adjustment layer
```

### File Diubah (minimal change):
```
config.py                      — Tambah 5 env vars Bitget
execution/paper_executor.py    — Tambah (BaseExecutor) inheritance
execution/live_executor.py     — Tambah (BaseExecutor) inheritance
core/user_session.py           — Factory switch berdasarkan EXECUTION_EXCHANGE
main.py                        — Init BitgetClient, inject ke sessions
                                  _update_positions() perlu price source switch
```

### File TIDAK BERUBAH:
```
engine/scoring_engine.py       ✅ Tetap 100% Hyperliquid
data/hyperliquid_client.py     ✅ Tidak diubah
data/ws_client.py              ✅ Tidak diubah
risk/risk_manager.py           ✅ Exchange-agnostic, tidak perlu ubah
models/schemas.py              ✅ Model sudah exchange-agnostic
```

---

*Dokumen ini adalah analisis teknis jujur. Semua kekurangan disebutkan secara eksplisit.*
*Keputusan implementasi ada di tangan developer/user berdasarkan kondisi aktual.*
