# Session Note - 2026-07-12 ML Data Pipeline

## Scope

- Audit ML KARA berdasarkan 225 experience row dan 220 outcome label Railway.
- Perbaikan data pipeline ML untuk scalper crypto perp.
- Model tetap observe-only.
- Tidak ada deploy Railway pada sesi ini.

## Temuan Data

- ML experience row: 225.
- Outcome label lengkap lama: 220.
- Minimum retrain: 300.
- Model belum memiliki data cukup untuk retrain baru.
- Expected edge lama berada pada range 0.5 sampai 0.6.
- Expected edge lama tidak membedakan setup lemah dan setup kuat.
- Model lama hanya belajar final win atau loss.
- Model lama tidak tahu entry location, micro invalidation, MFE, exit reason, atau time exit state.

## Risiko Model Lama

- Final win atau loss tidak cukup untuk mengukur kualitas entry scalper 1m.
- Trade dapat close hijau kecil tanpa impulse yang dibutuhkan strategi scalper.
- Trade dapat pernah spike lalu reverse, sehingga final result berbeda dari kualitas follow-through.
- Data lama tidak boleh dicampur sebagai feature lengkap untuk model baru.

## Target Baru

- Label baru bernama impulse win.
- Impulse win berarti trade close profit dan mencapai MFE minimal plus 0.35%.
- Nilai plus 0.35% sama dengan threshold early trail dan retest grace scalper.
- Target ini mengukur apakah entry menghasilkan follow-through yang cukup cepat untuk horizon scalper.

## Feature Entry Baru

- Trade mode.
- Entry location quality.
- Micro risk percentage.
- Micro risk adalah jarak entry ke invalidation microstructure.
- Feature lama tetap disimpan: score, meta delta, OI, liquidation, orderbook, session, funding, volatility, dan trend.

## Label Exit Baru

- Exit reason.
- Maximum favorable excursion atau MFE.
- Time exit trigger.
- Final ROE.
- Duration.
- Final win/loss.
- Impulse win.

## Training Rule Baru

- Model hanya train dari row dengan feature entry dan label exit lengkap.
- Minimum 300 outcome enriched.
- Data lama tetap disimpan untuk audit history.
- Data lama tidak dipakai untuk training feature contract baru.
- Model lama dengan feature contract lama dianggap stale dan tidak dipakai.
- Model tetap observe-only setelah training.
- Expected edge tidak menaikkan leverage dan tidak memblok trade pada sesi ini.

## Dashboard ML

- Progress training memakai jumlah outcome enriched, bukan total trade lama.
- Dashboard menjelaskan model sedang mengumpulkan feature entry dan exit lengkap.
- Dashboard tidak mengklaim AI memblok signal.
- Dashboard tetap menampilkan total trade, win/loss, score insight, dan recent results untuk observasi.

## File Changed

- `models/schemas.py`.
- `engine/scoring_engine.py`.
- `execution/paper_executor.py`.
- `execution/live_executor.py`.
- `intelligence/experience_buffer.py`.
- `intelligence/feature_engine.py`.
- `intelligence/intelligence_model.py`.
- `dashboard/app.py`.
- `dashboard/templates/dashboard.html`.
- `config.py`.

## Monitoring After Deploy

- Pantau enriched sample count pada dashboard.
- Pastikan new row punya trade mode, entry location, micro risk, exit reason, MFE, dan impulse win.
- Pantau distribution impulse win per score bucket dan entry location.
- Jangan gunakan expected edge sebagai gate sebelum minimum 300 outcome enriched dan calibration review.
- Setelah 300 sample, bandingkan prediction bucket dengan actual impulse win rate dan expectancy.
