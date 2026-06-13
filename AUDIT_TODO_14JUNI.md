# AUDIT TODO - 14 Juni 2026

Tujuan: melanjutkan perubahan sesi audit KARA untuk meningkatkan EV, bukan sekadar win rate. Fokus utama adalah membuat sinyal lebih prediktif, mengurangi false confidence, dan memisahkan setup futures yang secara kausal berbeda.

## Status Perubahan Yang Sudah Dilakukan

### 1. TP Scalper Dibuat Lebih Dekat

Metric:
- Time exit sering profit kecil tetapi TP lama terlalu jauh.
- Distribusi winner audit: p75 sekitar 0.57%, p90 sekitar 1.02%.

Pattern:
- Banyak trade tidak mencapai TP lama sebelum timeout.

Root Cause:
- Target profit tidak sesuai dengan konteks max hold pendek.

Action:
- `config.py`
- `tp1_pct`: 0.90% -> 0.50%
- `tp2_pct`: 1.20% -> 0.90%
- `trailing_pct`: 0.40% -> 0.30%

Expected Impact:
- Lebih banyak profit kecil diamankan sebelum momentum decay.
- Trade-off: sebagian runner besar bisa terpotong lebih cepat.

### 2. MTF 15m Tidak Lagi Menjadi Booster Besar Untuk Score Rendah

Metric:
- MTF align aktif pada mayoritas trade dan bucket `score <65` negatif EV.

Pattern:
- MTF terlalu sering aktif sehingga tidak diskriminatif.

Root Cause:
- 15m align lagging untuk execution 1m dengan max hold pendek.

Action:
- `config.py`
- Tambah:
  - `mtf_bonus_floor_score = 65`
  - `mtf_bonus_high_score = 72`
  - `mtf_mid_bonus = 4`
  - `mtf_high_bonus = 6`
- `engine/scoring_engine.py`
- MTF align sekarang hanya context bonus:
  - raw <65: +0
  - 65-71: +4
  - >=72: +6

Expected Impact:
- Mengurangi entry yang naik score hanya karena konfirmasi lagging.
- Trade-off: sebagian sinyal trend rendah tidak lagi lolos.

### 3. RSI Oversold Dan Overbought Dibedakan Secara Futures-Aware

Metric:
- RSI oversold LONG buruk: PnL negatif, PF rendah.
- RSI overbought LONG justru lebih baik, indikasi momentum continuation.

Pattern:
- Bot sebelumnya membaca oversold sebagai peluang LONG terlalu dini.

Root Cause:
- Di futures altcoin, oversold sering berarti forced selling masih berlangsung.

Action:
- `engine/scoring_engine.py`
- RSI oversold tidak lagi otomatis boost LONG.
- RSI oversold butuh minimal 2 konfirmasi:
  - CVD bullish
  - orderbook bid imbalance kuat
  - candle bullish follow-through
- Jika tidak confirmed, diberi catch-knife penalty.
- RSI overbought diperlakukan sebagai continuation jika EMA/momentum bullish.
- RSI overbought baru menjadi SHORT jika ada bearish orderflow/exhaustion.

Expected Impact:
- Mengurangi LONG yang menangkap pisau jatuh.
- Mempertahankan edge momentum continuation saat market kuat.

### 4. CVD Dibuat Contextual, Bukan Booster Hampir Selalu Aktif

Metric:
- CVD bullish muncul pada hampir semua trade sehingga tidak diskriminatif.

Pattern:
- CVD bullish tidak otomatis membedakan profit/loss.

Root Cause:
- CVD price-derived/orderflow-derived tanpa price follow-through bisa berarti absorption, bukan continuation.

Action:
- `engine/scoring_engine.py`
- CVD bullish + price naik + candle bullish -> valid LONG continuation.
- CVD bullish + price turun -> sell absorption risk, memberi poin bearish kecil.
- CVD bullish tanpa follow-through -> no boost.
- Logika simetris untuk CVD bearish.

Expected Impact:
- CVD menjadi filter kualitas, bukan noise yang menaikkan score semua trade.

### 5. Market Structure 1m Dibuat Lebih Ketat

Metric:
- Structure aktif pada semua trade sehingga tidak punya daya seleksi.

Pattern:
- Semua entry dianggap punya structure.

Root Cause:
- Definisi structure terlalu permisif.

Action:
- `engine/scoring_engine.py`
- Structure sekarang butuh:
  - break prior range
  - follow-through candle
- Bukan sekadar banyak close naik/turun.

Expected Impact:
- Mengurangi sinyal palsu yang hanya terlihat seperti HH/HL atau LH/LL lokal.

### 6. Tie Bias LONG Dihilangkan

Metric:
- Sistem sebelumnya bisa memilih LONG saat bull dan bear imbang.

Pattern:
- LONG menjadi default saat tidak ada directional edge.

Root Cause:
- Kondisi `bull_pts >= bear_pts` membuat tie otomatis LONG.

Action:
- `engine/scoring_engine.py`
- `bull_pts == bear_pts` sekarang menjadi reject/no-trade.
- Volume surge tidak lagi memecah tie ke LONG.

Expected Impact:
- Mengurangi trade tanpa edge arah.
- Trade-off: frekuensi sinyal turun, tetapi kualitas arah harus naik.

### 7. SHORT Dipisahkan Menjadi Dua Tesis

Metric:
- SHORT sample kecil tetapi lebih baik daripada LONG pada audit test.
- Kode sebelumnya mencampur breakdown continuation dan crowded-long reversal.

Pattern:
- Funding negatif pernah dianggap SHORT oleh analyzer, tetapi diblok oleh filter akhir.
- Funding positif bisa meloloskan SHORT, tetapi analyzer memberi poin LONG.

Root Cause:
- Sistem tidak membedakan dua jenis SHORT:
  - breakdown continuation
  - crowded-long reversal

Action:
- `engine/scoring_engine.py`
- SHORT valid jika salah satu tesis benar:
  - breakdown continuation: funding negatif + price turun + OI naik
  - crowded-long reversal: funding positif + price gagal naik + CVD/OB bearish
- `engine/analyzers/oi_funding_analyzer.py`
- Funding dibuat thesis-aware:
  - positive funding + price/OI expansion -> LONG continuation
  - positive funding + failed upside -> crowded-long reversal risk, SHORT
  - negative funding + price/OI expansion -> SHORT breakdown continuation
  - negative funding + failed downside -> crowded-short squeeze risk, LONG
  - funding tanpa price/OI thesis -> no directional boost

Expected Impact:
- Menghapus konflik internal analyzer vs final filter.
- SHORT tidak lagi buta funding.
- Trade-off: reversal SHORT tanpa CVD/OB bearish akan lebih sulit lolos.

### 8. Adaptive Entry Location Gate Untuk Scalper

Metric:
- Stop loss banyak terjadi cepat, sebelum trade punya waktu berkembang.

Pattern:
- Entry bisa lolos karena score arah, tetapi lokasi masuk belum selalu punya invalidation yang bersih.

Root Cause:
- Bot belum mengecek apakah entry dekat reclaim/support, rejection/resistance, atau breakout/retest yang masuk akal.

Action:
- `config.py`
- Tambah soft gate:
  - `entry_location_gate_enabled`
  - `entry_location_weak_penalty`
  - `entry_location_excellent_bonus`
  - `entry_location_weak_min_score`
- `engine/scoring_engine.py`
- Tambah `_validate_entry_location(...)`.
- Gate bersifat adaptive per regime:
  - ranging lebih ketat
  - trending lebih longgar
  - high_vol lebih hati-hati
  - extreme sangat selektif
- `invalid` hard reject.
- `weak` butuh score minimal lebih tinggi dan kena penalty.
- `excellent` mendapat bonus kecil.

Expected Impact:
- Mengurangi entry di lokasi acak yang mudah kena wick/SL.
- Tetap memberi ruang untuk breakout continuation valid pada regime trending.

### 9. Meta-Pattern Dan Asset Concentration Guard

Metric:
- Trade bisa berulang pada coin yang sama.
- Audit lokal menunjukkan `sig_meta_score_delta` aktif, tetapi `meta_boost` pada trade journal bisa kosong jika tidak dibawa ke `Position`.

Pattern:
- Meta bekerja di level signal, tetapi belum cukup kuat untuk mengurangi overtrading coin yang sama.

Root Cause:
- Scalper mengecek threshold sebelum meta penalty, lalu tidak re-check setelah score turun.
- Meta boost hanya melihat winrate, belum EV.
- `Position` belum menyimpan `meta_pattern_key` dan `meta_score_delta`.
- Belum ada guard untuk konsentrasi sinyal pada asset yang sama.

Action:
- `engine/scoring_engine.py`
  - Re-check threshold setelah meta-learning pada scalper.
  - Meta boost hanya jika WR tinggi dan `pnl_ema` positif.
  - Meta penalty jika WR rendah atau `pnl_ema` negatif.
  - Pattern key baru memakai score bucket, bukan hanya asset-side.
  - Legacy asset-side stats hanya dipakai untuk penalty jika EV buruk, bukan untuk boost.
  - Tambah asset concentration threshold add per asset/mode.
- `models/schemas.py`
  - Tambah `meta_pattern_key` dan `meta_score_delta` ke `Position`.
- `execution/paper_executor.py` dan `execution/live_executor.py`
  - Simpan meta field saat open/close position.

Expected Impact:
- Coin yang terlalu sering muncul butuh score lebih kuat.
- Pattern dengan WR tinggi tapi EV negatif tidak lagi di-boost.
- Audit meta berikutnya bisa membaca outcome lebih bersih.

### 10. Indicator Causality + Score Split

Metric:
- Audit komponen menunjukkan beberapa indikator tidak lagi diskriminatif jika dipakai mentah:
  - `sig_has_strong_imbalance=1`: PF sekitar `0.59`, WR sekitar `33%`.
  - SHORT dengan orderbook score negatif: PF sekitar `0.10`.
  - Raw score bucket tinggi: PF sekitar `0.44`.
  - CVD bullish sebelumnya terlalu sering muncul, sehingga menjadi noise.
  - MTF 15m align sebelumnya negatif pada score rendah.

Pattern:
- Score tinggi bisa terbentuk dari akumulasi indikator yang membaca kejadian sama.
- Orderbook wall/imbalance bisa menjadi trap jika price tidak bereaksi.
- CVD tanpa price follow-through tidak cukup sebagai sinyal arah.
- RSI oversold pada futures altcoin rawan menjadi catch-knife.
- MTF 15m lebih cocok sebagai konteks, bukan alasan utama entry scalper 1m.

Root Cause:
- Bot mencampur directional evidence dan trade quality dalam satu angka score.
- Beberapa indikator diperlakukan sebagai booster arah, padahal secara futures mereka harus dibaca secara kausal:
  - siapa yang agresif,
  - apakah harga merespons,
  - apakah orderflow benar-benar follow-through,
  - apakah entry punya invalidation yang masuk akal.

Action:
- `models/schemas.py`
  - Tambah `direction_score`, `trade_quality_score`, dan `failure_risk_score` ke `ScoreBreakdown`.
- `engine/scoring_engine.py`
  - Orderbook imbalance tidak langsung menjadi booster arah.
  - Bid/ask wall hanya memberi poin jika ada price reaction searah.
  - Jika orderbook kuat tapi price melawan, masuk `failure_risk_score`.
  - CVD bullish/bearish wajib punya price follow-through.
  - CVD bullish + price turun atau CVD bearish + price naik dianggap absorption risk.
  - RSI oversold tanpa orderflow confirmation menambah failure risk.
  - RSI overbought dipakai sebagai continuation context, bukan blind SHORT.
  - MTF align masuk `trade_quality_score`; MTF discord masuk `failure_risk_score`.
  - Final scalper score menjadi:

```text
final_score = direction_score + trade_quality_score - failure_risk_score
```

- `dashboard/app.py`
  - API `/api/trades` mengirim `direction_score`, `trade_quality_score`, dan `failure_risk_score`.

Expected Impact:
- Mengurangi false confidence dari orderbook/CVD mentah.
- Mengurangi LONG catch-knife dari RSI oversold.
- Membuat score tinggi lebih bermakna secara EV, bukan sekadar indikator menumpuk.
- Audit berikutnya bisa membedakan:
  - arah benar tapi entry buruk,
  - entry bagus tapi orderflow lemah,
  - setup kuat tapi failure risk tinggi.

Validation:
- Pastikan log signal berisi alasan `Score split`.
- Bandingkan sebelum vs sesudah:
  - PF orderbook-confirmed vs orderbook-unconfirmed,
  - CVD follow-through vs CVD absorption,
  - avg loss pada RSI oversold LONG,
  - distribusi `direction_score` vs `final_score`,
  - apakah stop loss rate turun tanpa membunuh EV.

## Pending / Belum Selesai

### 1. Format Telegram Time Exit

Status:
- Draft format sudah disetujui user.
- Implementasi sempat dicoba, tetapi file `notify/telegram.py` dikembalikan ke HEAD untuk menghindari mojibake/encoding churn.

TODO:
- Re-apply format pendek khusus `time_exit` dengan cara aman encoding.

Target format:

```text
🌸 KARA UPDATE: Time Exit

Saya menutup LONG VINE karena batas hold 12 menit tercapai.
Momentum mulai decay, jadi profit diamankan.

💰 Result
  • Profit : +$0.27 (+25.24% ROI)
  • Move   : +1.26%
  • Price  : $0.013419 → $0.013588

KARA lanjut memantau setup berikutnya. ✨
```

### 2. PnL Card / ROI Telegram

Status:
- Pernah dibahas bahwa persentase harus ROI on margin, bukan raw price move.
- Perlu cek ulang working tree sebelum implement karena `notify/telegram.py` sudah direstore.

TODO:
- Pastikan semua caption close memakai `% ROI`.
- Tambahkan `Move` sebagai raw price movement.
- Hindari double count `pnl + pnl_realized`.

### 3. Deploy Railway

Status:
- Belum deploy.

TODO:
- Jalankan compile/test lokal.
- Commit perubahan.
- Deploy ke Railway service test.
- Pantau minimal 20-50 sinyal/trade paper sebelum live.

### 4. Validasi EV Setelah Patch

TODO:
- Pull signal/trade terbaru dari Railway.
- Bandingkan sebelum vs sesudah:
  - expectancy
  - profit factor
  - avg win/loss
  - time_exit quality
  - stop_loss rate
  - LONG vs SHORT distribution
  - score bucket 60-64 vs 72+
  - thesis label breakdown vs reversal

## Checklist 14 Juni

- [ ] Re-apply Telegram Time Exit format pendek.
- [ ] Re-apply/fix ROI display pada Telegram close + PnL card jika belum ada.
- [ ] Jalankan `python -m py_compile engine\scoring_engine.py engine\analyzers\oi_funding_analyzer.py config.py notify\telegram.py notify\pnl_card.py`.
- [ ] Jalankan `git diff --check`.
- [ ] Review diff agar tidak ada encoding/mojibake churn.
- [ ] Commit perubahan dengan pesan jelas.
- [ ] Deploy ke Railway test service.
- [ ] Cek log Railway apakah alasan `Score split` muncul pada signal baru.
- [ ] Audit EV per `direction_score`, `trade_quality_score`, dan `failure_risk_score`.
- [ ] Audit orderbook confirmed vs unconfirmed setelah minimal 30-50 signal.
- [ ] Ambil data baru setelah bot berjalan.
- [ ] Audit apakah perubahan meningkatkan EV, bukan hanya win rate.

## Catatan Risiko

- Perubahan ini bisa menurunkan jumlah trade karena lebih banyak no-trade.
- SHORT reversal akan lebih selektif karena butuh failed upside + bearish flow.
- TP lebih dekat bisa mengurangi upside runner, tetapi lebih sesuai dengan distribusi move max hold pendek.
- Perubahan scoring perlu validasi out-of-sample dari Railway, bukan hanya backtest lokal.
