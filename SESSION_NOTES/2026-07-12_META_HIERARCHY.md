# Session Note - 2026-07-12 Meta Hierarchy

## Scope

- Audit meta pattern Railway berdasarkan 234 signal dan 220 closed trade.
- Implement meta hierarchy untuk mengurangi pattern netral akibat sample terpecah.
- Sesuaikan dashboard Meta Pattern agar menampilkan level evidence dan delta runtime.
- Tidak ada deploy Railway pada sesi ini.

## Temuan Data

- Signal dengan delta meta minus 12: 1.
- Signal dengan delta meta nol: 227.
- Signal dengan delta meta plus 8: 6.
- Meta netral pada 97.0% signal.
- Database memiliki 150 key meta untuk sekitar 220 closed trade.
- Rata-rata sample per key terlalu kecil untuk memory asset dan score bucket yang sangat spesifik.
- Boost lama tidak pernah mengangkat score di bawah gate native scalper 60.
- Penalty lama lebih besar daripada boost: minus 12 dibanding plus 8.
- Trade boost yang tersedia masih terlalu sedikit untuk membuktikan peningkatan edge.

## Masalah Lama

- Key lama memecah mode, asset, side, dan score bucket menjadi banyak pattern kecil.
- Sebagian besar pattern berhenti di sample 1 atau 2 dan tidak memberi keputusan.
- Specific pattern dengan sample 3 dapat memberi delta besar walau crypto perp 1m masih sangat noisy.
- Asset-side fallback lama dapat memberi penalty minus 12 tanpa level evidence yang jelas di dashboard.

## Desain Baru

- Specific key: mode, asset, side, score bucket.
- Asset-side key: mode, asset, side.
- Side-bucket key: mode, side, score bucket.
- Side key: mode, side.
- Semua level menerima outcome close baru.
- Specific level dapat memberi boost atau penalty.
- Aggregate level hanya boleh memberi penalty kecil.
- Aggregate level tidak pernah memberi boost.
- Native scorer 1m tetap menjadi gate utama entry.

## Delta Baru

- Specific pattern kuat dengan minimal 5 sample: plus 5.
- Specific pattern buruk dengan minimal 5 sample: minus 7.
- Asset-side buruk dengan minimal 5 sample: minus 4.
- Side-bucket buruk dengan minimal 20 sample: minus 3.
- Side buruk dengan minimal 30 sample: minus 2.
- Meta delta dibatasi maksimal plus atau minus 7.

## Alasan Parameter

- Plus 5 cukup mengubah prioritas score 64 menjadi 69 tanpa mengubah setup sub-60 menjadi entry otomatis.
- Minus 7 menahan repeated low-EV specific setup tanpa menghapus semua setup borderline karena noise.
- Minus 4 dan lebih kecil dipakai untuk aggregate karena evidence kurang spesifik terhadap asset dan entry location.
- Minimum 5 specific sample lebih kuat dari minimum lama 3 sample.
- Minimum 20 dan 30 dipakai untuk aggregate agar bias side crypto perp tidak terbentuk dari beberapa loss saja.

## Dashboard Baru

- Dashboard menunjukkan level: specific, asset-side, side-bucket, atau side.
- Dashboard menunjukkan delta runtime nyata: plus 5, minus 7, minus 4, minus 3, minus 2, atau observe.
- Dashboard menunjukkan sample saat ini dibanding sample minimum pada level tersebut.
- Dashboard membedakan boost aktif, penalty aktif, dan observe.
- Dashboard tidak lagi memberi label boost sebelum evidence cukup.

## File Changed

- `config.py`.
- `core/db.py`.
- `engine/scoring_engine.py`.
- `dashboard/app.py`.
- `dashboard/templates/dashboard.html`.
- `tests/test_meta_hierarchy.py`.

## Monitoring After Deploy

- Pantau proporsi signal delta nol sebelum dan sesudah data aggregate terbentuk.
- Pantau jumlah specific boost dan specific penalty.
- Pantau jumlah penalty asset-side, side-bucket, dan side.
- Pantau PnL trade menurut meta delta.
- Pantau pattern specific dengan minimum 5 sample yang berubah status.
- Audit ulang sesudah minimal 100 closed trade baru.
- Jangan memperbesar boost sebelum sample boost baru cukup.
