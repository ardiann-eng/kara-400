Root Cause 1: Grace Period Memegang Loser Lebih Lama
Ini root cause paling jelas dari database plus code.
Scalper config:
max_hold_minutes = 12.0
max_hold_grace_minutes = 6.0
max_hold_soft_floor_pct = -0.0015
config.py:259-265.
Di risk/risk_manager.py:894-896:
elif floating <= soft_floor / 100.0 and hold_minutes < (max_hold + grace):
    pass  # grace period — wait
Maknanya:
Jika trade sudah rugi lebih dari -0.15%:
bot memberi waktu tambahan sampai 18 menit.
Jadi bot melakukan hal berlawanan dengan exit discipline yang sehat:
winner cepat: sering ditutup/trail sekitar 12 menit
loser nyata: diberi grace sampai 18 menit
Bukti kara_ml.db:
Duration	Long n	Long WR	Long Avg ROE	Short n	Short WR
<12m	50	82.0%	+2.47%	7	71.4%
12-18m	80	96.2%	+3.34%	8	87.5%
18m+	68	26.5%	-0.33%	7	14.3%
Kelompok 18m+ adalah kelompok jelek.
Untuk short:
7 trade bertahan 18m+
WR 14.3%
Avg ROE -5.08%
Median ROE -6.17%
Untuk long:
68 trade bertahan 18m+
WR 26.5%
Median ROE -4.28%
Root cause: grace period dipicu oleh loss, bukan oleh thesis yang masih valid.
Perbaikan konkret:
Jangan hapus grace. Ubah syarat grace.
Grace hanya boleh ada bila semua ini benar:
1. Higher-timeframe trend masih searah.
2. 1m structure belum invalid.
3. Price masih di atas/bawah invalidation microstructure.
4. Spread tidak melebar.
5. Tidak ada momentum berlawanan baru.
6. MAE belum melewati batas loss-state khusus.
Jika floating loss sudah melewati -0.15% tetapi structure invalid, close segera. Jangan beri tambahan 6 menit hanya karena trade sedang merah.
Target code: risk/risk_manager.py:875-907.

Root Cause 3: Time Exit Punya Win Rate Positif Tapi Expectancy Negatif
132 time_exit:
WR       57.6%
Net PnL  -$25.18
Avg PnL  -$0.19
Median   +$0.06
Median positif tapi total negatif. Ini berarti banyak kemenangan kecil tidak menutup sedikit loss besar.
Pola payoff:
time_exit: kecil-kecil, cenderung scratch
stop_loss: loss besar
trailing: winner besar
Bot belum cukup selektif membedakan:
trade yang hanya bergerak sedikit
trade yang benar-benar punya expansion
Perbaikan konkret:
Tambah state entry dan state exit:
impulse_confirmed
retest_holding
no_follow_through
microstructure_invalid
trend_expanding
adverse
Aturan:
no_follow_through:
  keluar lebih awal, sebelum loss membesar

microstructure_invalid:
  keluar sebelum SL penuh

retest_holding:
  boleh grace bila HTF dan 1m tetap valid

trend_expanding:
  trailing mengambil alih, max-hold tidak memotong winner

adverse:
  jangan beri grace otomatis
Ini bukan menaikkan waktu hold global. Ini membuat grace berdasarkan kondisi pasar.