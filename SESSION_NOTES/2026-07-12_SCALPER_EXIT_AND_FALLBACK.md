# Session Note - 2026-07-12

## Scope

- Audit database Railway untuk paper trading KARA.
- Perbaikan hanya untuk grace period scalper dan time exit berbasis market state.
- Tambahan revalidation native 1m untuk standard fallback sebelum entry scalper.
- Tidak ada deploy Railway pada sesi ini.

## Data Source

- Database Railway: `/data/kara_data.db`.
- Database ML Railway: `/data/kara_ml.db`.
- Database integrity check: `ok`.
- Closed trade yang dianalisis: 220.
- Signal tersimpan: 229.
- ML outcome label tersimpan: 220.

## Audit Exit

- Time exit: 132 trade, win rate 57.6%, net PnL minus 25.18 USD.
- Stop loss: 17 trade, win rate 29.4%, net PnL minus 27.89 USD.
- Trailing stop: 50 trade, win rate 100%, net PnL plus 69.26 USD.
- Time exit punya win rate positif tetapi expectancy negatif.
- Banyak kemenangan kecil tidak menutup loss yang lebih besar.

## Audit Hold Time

- Long di bawah 12 menit: 50 trade, win rate 82.0%, average ROE plus 2.47%.
- Long 12 sampai 18 menit: 80 trade, win rate 96.2%, average ROE plus 3.34%.
- Long di atas 18 menit: 68 trade, win rate 26.5%, median ROE minus 4.28%.
- Short di atas 18 menit: 7 trade, win rate 14.3%, average ROE minus 5.08%.
- Kesimpulan: window 12 sampai 18 menit valid untuk continuation yang sehat.
- Kesimpulan: posisi di atas 18 menit cenderung menjadi loser drift.

## Root Cause 1

- Grace lama dipicu hanya oleh kondisi floating loss.
- Posisi merah dapat bertahan sampai 18 menit tanpa bukti thesis 1m masih valid.
- Ini memberi waktu lebih lama kepada loser dibanding trade yang gagal follow-through.

## Root Cause 3

- Time exit lama tidak membedakan retest sehat dengan momentum yang sudah gagal.
- Bot belum memakai invalidation microstructure entry saat mengelola posisi.
- Bot belum memakai keadaan EMA21 dan momentum close 1m pada window keputusan 10 sampai 18 menit.

## Perubahan Exit State

- Tambah micro invalidation price pada signal scalper.
- Simpan micro invalidation price pada posisi paper dan live.
- Tambah state market untuk posisi scalper umur 10 sampai 18 menit.
- State market memakai EMA21 1m, tiga close terakhir, invalidation entry, dan trend entry.
- State market hanya diminta pada window keputusan agar tidak menambah beban API untuk semua posisi.
- Grace 12 sampai 18 menit hanya untuk trade yang pernah punya impulse minimal plus 0.35%.
- Grace hanya untuk trade yang floating PnL masih minimal minus 0.15%.
- Grace hanya untuk trade dengan structure 1m valid.
- Grace hanya untuk trade dengan trend entry masih searah.
- Grace ditolak bila momentum 1m berlawanan.
- Posisi umur minimal 10 menit keluar lebih awal bila loss minimal minus 0.30% dan structure 1m patah atau momentum berlawanan.
- Stop loss utama tetap aktif.
- Trailing winner tetap lebih prioritas daripada time exit.
- Force exit tetap berlaku pada 18 menit.

## Parameter Baru

- Market state check dimulai pada menit ke-10.
- Grace retest perlu MFE minimal plus 0.35%.
- Early adverse exit perlu loss minimal minus 0.30%.
- Grace masih mengizinkan retest ringan sampai minus 0.15%.
- Parameter dipilih di antara noise normal 1m crypto perp dan stop scalper 0.80%.
- Parameter tidak dimaksudkan sebagai hard stop baru.

## Audit Native Scalper Versus Standard Fallback

- Native scalper: 178 trade, win rate 68.5%, net PnL plus 37.93 USD.
- Standard fallback: 46 trade, win rate 65.2%, net PnL minus 0.35 USD.
- Native scalper stop loss average minus 1.38 USD.
- Standard fallback stop loss average minus 2.12 USD.
- Standard fallback score 60 sampai 64: 29 trade, net PnL minus 5.21 USD.
- Native scalper score 60 sampai 64: 51 trade, net PnL plus 6.47 USD.
- Standard fallback short: 23 trade, net PnL minus 6.28 USD.
- Standard fallback long: 23 trade, net PnL plus 5.93 USD.

## Perubahan Standard Fallback

- Standard signal sekarang hanya menjadi directional candidate.
- Standard fallback tidak lagi langsung memakai risk profile scalper.
- Bot menjalankan ulang native scalper 1m untuk asset yang sama.
- Native scalper wajib menghasilkan signal valid.
- Arah native scalper wajib sama dengan arah standard signal.
- Entry memakai signal native scalper yang baru.
- Entry memakai level SL, TP, volatility, MTF, spread, dan entry-location native scalper.
- Jika native 1m reject, fallback diblok.
- Jika arah native 1m berbeda, fallback diblok.
- Fallback tetap ada untuk menangkap context standard yang mendapat confirmation microstructure.

## File Changed

- `config.py`.
- `models/schemas.py`.
- `engine/scoring_engine.py`.
- `risk/risk_manager.py`.
- `main.py`.
- `execution/paper_executor.py`.
- `execution/live_executor.py`.
- `tests/test_scalper_exit_state.py`.

## Verification

- Python compile berhasil untuk file yang diubah.
- `git diff --check` tidak menemukan whitespace error.
- Test khusus dibuat untuk retest grace, no-follow-through exit, dan microstructure invalid exit.
- Test runner belum dapat berjalan di local environment karena dependency `pytest`, `pydantic`, dan `pandas` belum tersedia.

## Monitoring After Deploy

- Pantau jumlah log `SCALPER-FALLBACK-BLOCK`.
- Pantau jumlah log `SCALPER-FALLBACK-CONFIRMED`.
- Pisahkan outcome native scalper dan standard fallback pada audit berikutnya.
- Pantau time exit kategori 10 sampai 12 menit, 12 sampai 18 menit, dan di atas 18 menit.
- Pantau early exit microstructure invalid dibanding stop loss penuh.
- Audit ulang setelah minimal 100 closed trade baru.
- Jangan ubah parameter sebelum sample baru cukup.
