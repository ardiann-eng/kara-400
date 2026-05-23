# KARA — Early Pump Detection Gate

## Problem Statement

Bot masuk SETELAH pump terjadi (lagging indicators: EMA cross, CVD confirms, momentum 0.15%+).
Harga sudah naik → bot entry → harga diam → timeout 12 menit → loss.

**Data Audit #9:**
- 62% trades = time_exit (harga tidak gerak setelah entry)
- Trailing stop hanya fire 33% — harusnya lebih tinggi kalau entry timing benar
- Avg time_exit loss = -$0.52 × 83 trades = -$42.89

## Solusi: Volume Surge + Early Move Gate

Ganti filosofi entry dari "score tinggi" ke "pump sedang dimulai".

### Definisi "Pump Baru Mulai"

Tiga kondisi harus terpenuhi BERSAMAAN:

```
1. VOLUME SURGE:    vol_recent (5 candle) > 2.0× vol_baseline (30 candle)
2. PRICE ACCEL:     |candle_last| > 1.5× avg_candle_size (10 candle)
3. NOT TOO LATE:    total_move_5m < 0.5% (masih ada room untuk lanjut)
```

### Kenapa Ini Bekerja

| Kondisi | Apa yang dideteksi |
|---|---|
| Volume surge | Smart money / whale BARU masuk. Liquidity event dimulai. |
| Price acceleration | Harga MULAI respond terhadap volume. Bukan sideways. |
| Not too late | Masih ada 0.5-1.0% room ke TP1 (0.85%). Belum exhausted. |

### Apa yang Di-block

| Situasi | Volume | Price | Move | Result |
|---|---|---|---|---|
| Market diam | 1× baseline | kecil | <0.1% | ❌ BLOCK — no energy |
| Pump sudah selesai | 2×+ | kecil (sudah slow) | >0.5% | ❌ BLOCK — too late |
| Pump baru mulai | 2×+ | besar (accelerating) | <0.5% | ✅ PASS — ride it |
| Fake spike (1 candle) | 1.5× | besar | <0.2% | ❌ BLOCK — vol belum confirm |

## Arsitektur Perubahan

### File: `engine/scoring_engine.py`

**Lokasi:** Setelah momentum gate, sebelum threshold check (~line 700-740)

```python
# ── PUMP TIMING GATE ──────────────────────────────────────
# Hanya entry saat volatility EXPANDING (pump baru mulai)
# Data Audit #9: 62% time_exit karena entry setelah pump selesai

volumes = [float(c.get('v', 0)) for c in candles[-35:]]  # 35 candle 1m
if len(volumes) >= 35:
    vol_baseline = sum(volumes[-35:-5]) / 30          # avg vol 30 candle lalu
    vol_recent = sum(volumes[-5:]) / 5                # avg vol 5 candle terakhir
    vol_surge_ratio = vol_recent / max(vol_baseline, 1e-10)

    # Price acceleration: candle terakhir vs avg candle size
    candle_sizes = [abs(closes[i] - closes[i-1]) / closes[i-1] for i in range(-10, 0)]
    avg_candle = sum(candle_sizes) / len(candle_sizes)
    last_candle = abs(closes[-1] - closes[-2]) / closes[-2]
    price_accel = last_candle / max(avg_candle, 1e-10)

    # Total move last 5 candles (sudah berapa jauh?)
    move_5m = abs(closes[-1] - closes[-6]) / closes[-6]

    pump_starting = (
        vol_surge_ratio >= 2.0 and    # volume 2× baseline
        price_accel >= 1.5 and         # candle accelerating
        move_5m < 0.005                 # belum gerak > 0.5%
    )

    if not pump_starting:
        # BLOCK — tidak ada pump, atau pump sudah selesai
        return None, score
```

### Data yang Dibutuhkan

| Data | Source | Status |
|---|---|---|
| 1m candle volumes | `self.cache.candles[asset]` | ✅ Sudah ada |
| 1m close prices | `closes` variable | ✅ Sudah ada |
| Minimal 35 candle history | WS feed | ✅ Sudah ada (bot collect sejak start) |

**Tidak perlu data baru.** Semua sudah tersedia di cache.

### Interaction dengan Filter Lain

```
Filter chain (urutan):
1. Spread > 0.15% → reject
2. EXTREME regime → skip
3. Score < threshold → skip
4. Momentum gate (dir_move ≥ 0.15%) → skip          ← TETAP ADA
5. ★ PUMP TIMING GATE (vol surge + accel + not late) → skip  ← BARU
6. Momentum confirmation (candles)
7. Displacement penalty
```

Pump gate SETELAH score threshold — jadi hanya evaluate trade yang sudah punya score decent.
Momentum gate (0.15%) tetap ada sebagai minimum floor.

### Expected Impact

| Metric | Sekarang | Target |
|---|---|---|
| Trades/day | ~98 | ~35-50 |
| time_exit % | 62% | <35% |
| trailing_stop % | 33% | >50% |
| PF | 1.027 | >1.5 |
| PnL/day | +$1.24 | +$10-20 |

### Tuning Parameters

| Parameter | Default | Aggressive | Conservative |
|---|---|---|---|
| `vol_surge_min` | 2.0× | 1.5× (more trades) | 3.0× (fewer, higher quality) |
| `price_accel_min` | 1.5× | 1.2× | 2.0× |
| `max_move_5m` | 0.5% | 0.7% (allow later entry) | 0.3% (very early only) |

Start dengan default. Tune berdasarkan data audit berikutnya.

### Risiko & Mitigasi

| Risiko | Mitigasi |
|---|---|
| Terlalu sedikit trade (< 20/day) | Turunkan vol_surge ke 1.5× |
| Miss pump yang gradual (slow build) | price_accel 1.5× sudah cukup rendah |
| False positive (volume spike tapi no follow-through) | "Not too late" gate + trailing stop sebagai safety net |
| Coin low-liquidity volume spike noise | Sudah di-filter oleh spread gate (0.15%) |

### Verification Plan

Setelah deploy, audit 24 Mei cek:
1. Berapa trade di-block oleh pump gate? (target: 40-60% of old trades)
2. Trailing fire rate naik? (target: >45%)
3. time_exit turun? (target: <40%)
4. PnL per trade naik? (target: avg > +$0.20)

### Rollback

Kalau trailing fire rate TURUN (< 25%) → pump gate terlalu ketat → turunkan threshold atau disable.
