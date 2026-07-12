# Session Note - 2026-07-12 Pre-TP1 Profit Lock

## Scope

- Audit trailing stop, TP1, TP2, dan time exit dari database Railway dan code KARA.
- Ubah hanya mekanisme pre-TP1 scalper.
- Post-TP1 trailing tidak diubah.
- Tidak ada deploy Railway pada sesi ini.

## Data Audit

- Trailing stop: 54 trade, net PnL plus 74.33 USD, average plus 1.38 USD.
- Time exit: 138 trade, net PnL minus 25.35 USD, average minus 0.18 USD.
- Stop loss: 17 trade, net PnL minus 27.89 USD, average minus 1.64 USD.
- Trailing adalah sumber profit besar KARA dan tidak boleh dilonggarkan secara global.
- 10 dari 54 trailing close memberi PnL maksimal 0.50 USD.
- 7 dari 54 trailing close memberi PnL maksimal 0.25 USD.

## Root Cause

- Scalper early trail arm pada plus 0.40%.
- Scalper TP1 pada plus 0.45%.
- Selisih hanya 0.05%.
- Early trail lama memakai full close saat retrace 0.25% dari peak.
- Trade dapat mencapai plus 0.40%, arm trailing, lalu close seluruh posisi sebelum menyentuh TP1.
- Trade tersebut tidak memperoleh partial TP1 dan tidak menyisakan runner.

## Perubahan

- Early trail sebelum TP1 dihapus sebagai full-close trigger.
- Saat MFE mencapai plus 0.40%, bot memindahkan stop ke entry plus 0.05% untuk long.
- Saat MFE mencapai plus 0.40%, bot memindahkan stop ke entry minus 0.05% untuk short.
- Status posisi menyimpan `early_profit_lock`.
- Harga masih diberi kesempatan mencapai TP1 plus 0.45%.
- Saat TP1 kena, bot tetap close partial sesuai ratio scalper 60%.
- Setelah TP1, trailing sisa posisi tetap memakai mekanisme existing.
- Stop profit-lock tetap diberi reason `profit_lock_stop` agar audit tidak mencampurnya dengan stop loss asli.

## Telemetry Baru

- Trade final menyimpan `tp1_hit`.
- Trade final menyimpan `tp2_hit`.
- Trade final menyimpan `early_profit_lock`.
- Trade final menyimpan `max_floating_pct`.
- Metric ini dipakai untuk audit TP1 hit rate, TP2 hit rate, profit lock rate, dan trailing after TP1.

## Label Notification dan Audit

- Stop awal yang benar-benar merugi tetap memakai reason `stop_loss`.
- Stop yang sudah berada di atas entry long atau di bawah entry short memakai reason `profit_lock_stop`.
- Telegram menampilkan `Profit Lock`, bukan `Stop Loss`, untuk exit setelah proteksi profit aktif.
- PnL card menampilkan `FULL · LOCK` dengan warna profit.
- Audit berikut dapat memisahkan stop loss asli, profit lock setelah impulse atau TP1, trailing stop, dan time exit.

## Expected Effect

- Mengurangi trailing crumb sebelum TP1.
- Menjaga trade yang sudah punya impulse agar tidak berubah loss.
- Menambah peluang TP1 dan TP2 tanpa membuat post-TP1 trailing lebih longgar.
- Runner tetap mengikuti trailing yang sudah menghasilkan net PnL positif.

## File Changed

- `models/schemas.py`.
- `risk/risk_manager.py`.
- `execution/paper_executor.py`.
- `execution/live_executor.py`.
- `tests/test_scalper_exit_state.py`.

## Monitoring After Deploy

- Bandingkan rate `early_profit_lock` dengan TP1 hit rate.
- Pantau PnL exit `profit_lock_stop`.
- Pantau trailing close sebelum dan sesudah TP1.
- Pantau TP2 hit rate.
- Audit ulang setelah minimal 100 closed scalper trade baru.
- Jangan ubah post-TP1 trail sebelum metric baru cukup.
