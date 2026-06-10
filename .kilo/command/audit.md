---
description: Audit strategi trading — evaluasi win rate, profit factor, drawdown, edge sustainability, dan rekomendasi perbaikan.
agent: kara-quant
---

Jalankan audit trading pada data terbaru KARA.

## Yang Harus Dievaluasi

### Tier 1: System Integrity
1. Score validity: correlation score vs PnL
2. Komponen conflict: ada leading signal yang kontradiksi?
3. Dead code: ada data di-fetch tapi tidak digunakan?
4. Threshold consistency: semua gate sinkron?
5. Component firing rate: 0% atau 100% → flag

### Tier 2: Performance
6. Exit reason breakdown: trailing_stop vs time_exit
7. Momentum vs outcome: dir_move >0.15% outperform?
8. Score decile: tidak boleh inverse
9. Per-coin concentration analysis

### Tier 3: Optimization
10. Fee impact analysis
11. Rekomendasi perbaikan dengan data pendukung

## Output Format

1. **Ringkasan** — metrik utama (WR, PF, PnL, trades, drawdown)
2. **Temuan Utama** — apa yang berubah dari audit sebelumnya
3. **Risiko** — apa yang perlu diwaspadai
4. **Rekomendasi** — action items dengan format `[what] → [why] → [how to verify]`
