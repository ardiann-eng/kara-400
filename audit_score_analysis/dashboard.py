"""
KARA Score Audit — Phase 3: Generate Plotly dashboard.
Reads pickled DataFrames from analyze.py and produces a single HTML dashboard.
"""
import json
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
from plotly.io import to_html
from sklearn.ensemble import RandomForestClassifier

OUT = "audit_score_analysis"

df          = pd.read_pickle(f"{OUT}/data/df.pkl")
imp_df      = pd.read_pickle(f"{OUT}/data/imp.pkl")
corr_df     = pd.read_pickle(f"{OUT}/data/corr.pkl")
corr_out_df = pd.read_pickle(f"{OUT}/data/corr_out.pkl")
decile      = pd.read_pickle(f"{OUT}/data/decile.pkl")
exit_perf   = pd.read_pickle(f"{OUT}/data/exit_perf.pkl")
with open(f"{OUT}/data/summary.json") as f:
    summary = json.load(f)

DARK_BG = "#1a1a1a"
PANEL   = "#2a2a2a"
TXT     = "#eeeeee"
GREEN   = "#22c55e"
RED     = "#ef4444"
BLUE    = "#3b82f6"
YELLOW  = "#facc15"

def _layout(fig, title, height=420):
    fig.update_layout(
        title=dict(text=title, font=dict(color=TXT, size=16)),
        paper_bgcolor=PANEL, plot_bgcolor=PANEL, font=dict(color=TXT),
        height=height, margin=dict(l=50, r=20, t=50, b=40),
        legend=dict(bgcolor=PANEL, bordercolor="#444", borderwidth=1),
    )
    fig.update_xaxes(gridcolor="#333", zerolinecolor="#444")
    fig.update_yaxes(gridcolor="#333", zerolinecolor="#444")
    return fig

def html_div(fig):
    return to_html(fig, include_plotlyjs=False, full_html=False, div_id=None)

# ─────────────────────────────────────────────────────────────────
# CHART 1: Feature importance (native + permutation)
# ─────────────────────────────────────────────────────────────────
top = imp_df.head(15).iloc[::-1]
fig1 = make_subplots(rows=1, cols=2, subplot_titles=("Native RF Importance", "Permutation Importance (test set)"))
fig1.add_trace(go.Bar(
    x=top["native"], y=top["feature"], orientation="h",
    marker_color=BLUE, name="Native"
), row=1, col=1)
fig1.add_trace(go.Bar(
    x=top["permutation"], y=top["feature"], orientation="h",
    marker_color=YELLOW, name="Permutation",
    error_x=dict(type="data", array=top["permutation_std"], color=TXT, thickness=1)
), row=1, col=2)
_layout(fig1, "Chart 1 — Feature Importance: Win/Loss Prediction (top 15)", height=560)

# ─────────────────────────────────────────────────────────────────
# CHART 2: Score vs PnL scatter
# ─────────────────────────────────────────────────────────────────
fig2 = go.Figure()
for outcome, color, name in [(1, GREEN, "Win"), (0, RED, "Loss")]:
    sub = df[df["outcome"] == outcome]
    fig2.add_trace(go.Scatter(
        x=sub["sig_score"], y=sub["pnl_usd"],
        mode="markers", marker=dict(color=color, opacity=0.65, size=7),
        name=name, hovertemplate="score=%{x}<br>pnl=$%{y:.2f}<extra></extra>",
    ))
# trend line
z = np.polyfit(df["sig_score"], df["pnl_usd"], 1)
xline = np.linspace(df["sig_score"].min(), df["sig_score"].max(), 50)
fig2.add_trace(go.Scatter(
    x=xline, y=np.poly1d(z)(xline), mode="lines",
    line=dict(color=YELLOW, dash="dash", width=2), name=f"Trend (slope={z[0]:.4f})"
))
r = df["sig_score"].corr(df["pnl_usd"])
fig2.update_layout(xaxis_title="Signal Score at Entry", yaxis_title="Realized PnL ($)")
_layout(fig2, f"Chart 2 — Signal Score vs Realized PnL (Pearson r = {r:.3f})", height=480)

# ─────────────────────────────────────────────────────────────────
# CHART 3: Score distribution by outcome
# ─────────────────────────────────────────────────────────────────
fig3 = go.Figure()
fig3.add_trace(go.Histogram(
    x=df[df["outcome"] == 1]["sig_score"], name="Win",
    marker_color=GREEN, opacity=0.65, nbinsx=25
))
fig3.add_trace(go.Histogram(
    x=df[df["outcome"] == 0]["sig_score"], name="Loss",
    marker_color=RED, opacity=0.65, nbinsx=25
))
fig3.update_layout(xaxis_title="Signal Score", yaxis_title="Count", barmode="overlay")
_layout(fig3, "Chart 3 — Score Distribution: Win vs Loss", height=420)

# ─────────────────────────────────────────────────────────────────
# CHART 4: Correlation heatmap (numeric features + outcome + pnl)
# ─────────────────────────────────────────────────────────────────
heat_features = [c for c in [
    "sig_score", "sig_session_bonus", "sig_realized_vol",
    "sig_entry_atr", "sig_funding_rate", "sig_suggested_leverage",
    "sig_n_reasons", "sig_has_rsi_oversold", "sig_has_rsi_overbought",
    "sig_has_rsi_divergence", "sig_has_volume_surge", "sig_has_mtf_align",
    "sig_has_ny_session", "sig_has_london_session", "sig_has_funding_extreme",
    "outcome", "pnl_usd",
] if c in df.columns]
heat_df = df[heat_features].apply(pd.to_numeric, errors="coerce").fillna(0)
corr_matrix = heat_df.corr().round(2)
fig4 = px.imshow(
    corr_matrix, text_auto=True, color_continuous_scale="RdBu_r",
    zmin=-1, zmax=1, aspect="auto",
)
_layout(fig4, "Chart 4 — Feature Correlation Matrix", height=600)
fig4.update_layout(coloraxis_colorbar=dict(title="ρ"))

# ─────────────────────────────────────────────────────────────────
# CHART 5: Component contribution (means + zero-variance flag)
# ─────────────────────────────────────────────────────────────────
comp_features = ["sig_oi_funding_score", "sig_liquidation_score",
                 "sig_orderbook_score", "sig_session_bonus"]
comp_means = [df[f].mean() if f in df.columns else 0 for f in comp_features]
comp_stds  = [df[f].std()  if f in df.columns else 0 for f in comp_features]
comp_labels = ["OI+Funding", "Liquidation", "Orderbook", "Session Bonus"]
colors = [RED if s == 0 else GREEN for s in comp_stds]

fig5 = make_subplots(rows=1, cols=2, subplot_titles=("Mean Contribution", "Std (variance)"))
fig5.add_trace(go.Bar(
    x=comp_labels, y=comp_means, marker_color=colors,
    text=[f"{v:.2f}" for v in comp_means], textposition="outside",
    name="Mean"
), row=1, col=1)
fig5.add_trace(go.Bar(
    x=comp_labels, y=comp_stds, marker_color=colors,
    text=[f"{v:.2f}" for v in comp_stds], textposition="outside",
    name="Std"
), row=1, col=2)
_layout(fig5, "Chart 5 — Score Component Contribution (red = zero variance = analyzer not firing!)", height=440)

# ─────────────────────────────────────────────────────────────────
# CHART 6: Exit reason performance
# ─────────────────────────────────────────────────────────────────
fig6 = make_subplots(rows=1, cols=3,
                     subplot_titles=("Trade Count", "Win Rate %", "Total PnL ($)"))
fig6.add_trace(go.Bar(
    x=exit_perf["exit_reason"], y=exit_perf["n"],
    marker_color=BLUE, text=exit_perf["n"], textposition="outside"
), row=1, col=1)
wr_colors = [GREEN if w >= 0.5 else RED for w in exit_perf["win_rate"]]
fig6.add_trace(go.Bar(
    x=exit_perf["exit_reason"], y=exit_perf["win_rate"] * 100,
    marker_color=wr_colors, text=[f"{w*100:.0f}%" for w in exit_perf["win_rate"]],
    textposition="outside"
), row=1, col=2)
pnl_colors = [GREEN if p > 0 else RED for p in exit_perf["total_pnl"]]
fig6.add_trace(go.Bar(
    x=exit_perf["exit_reason"], y=exit_perf["total_pnl"],
    marker_color=pnl_colors, text=[f"${p:.1f}" for p in exit_perf["total_pnl"]],
    textposition="outside"
), row=1, col=3)
fig6.update_layout(showlegend=False)
_layout(fig6, "Chart 6 — Performance by Exit Reason", height=440)

# ─────────────────────────────────────────────────────────────────
# CHART 7: Score decile analysis
# ─────────────────────────────────────────────────────────────────
fig7 = make_subplots(specs=[[{"secondary_y": True}]])
decile_labels = [f"D{int(d)}\n[{int(r['score_min'])}–{int(r['score_max'])}]"
                 for d, r in zip(decile["score_decile"], decile.to_dict("records"))]
fig7.add_trace(go.Bar(
    x=decile_labels, y=decile["win_rate"] * 100,
    marker_color=[GREEN if wr >= 0.5 else RED for wr in decile["win_rate"]],
    name="Win Rate %",
    text=[f"{wr*100:.0f}%<br>n={n}" for wr, n in zip(decile["win_rate"], decile["n"])],
    textposition="outside"
), secondary_y=False)
fig7.add_trace(go.Scatter(
    x=decile_labels, y=decile["avg_pnl"],
    mode="lines+markers",
    line=dict(color=YELLOW, width=3),
    marker=dict(size=10),
    name="Avg PnL ($)"
), secondary_y=True)
fig7.add_hline(y=50, line_dash="dot", line_color="#888", secondary_y=False)
fig7.add_hline(y=0, line_dash="dot", line_color="#888", secondary_y=True)
fig7.update_xaxes(title_text="Score Decile (lowest → highest)")
fig7.update_yaxes(title_text="Win Rate (%)", secondary_y=False, range=[0, 110])
fig7.update_yaxes(title_text="Avg PnL ($)", secondary_y=True)
_layout(fig7, "Chart 7 — Score Decile Analysis: WR & Avg PnL", height=480)

# ─────────────────────────────────────────────────────────────────
# CHART 8: Feature importance per exit type (heatmap)
# ─────────────────────────────────────────────────────────────────
features = [c for c in df.columns if c.startswith("sig_") and pd.api.types.is_numeric_dtype(df[c])]
features = [f for f in features if df[f].std() > 0]  # exclude zero-variance

exit_types = exit_perf["exit_reason"].tolist()
heat_data = []
heat_index = []
for et in exit_types:
    sub = df[df["exit_reason"] == et]
    if len(sub) < 10:
        continue
    rf_t = RandomForestClassifier(n_estimators=100, max_depth=6, random_state=42, n_jobs=-1)
    try:
        rf_t.fit(sub[features].fillna(0), sub["outcome"])
        heat_data.append(pd.Series(rf_t.feature_importances_, index=features))
        heat_index.append(f"{et} (n={len(sub)})")
    except Exception:
        pass

if heat_data:
    heat_df = pd.DataFrame(heat_data, index=heat_index).fillna(0)
    # only keep top 12 features overall
    top_overall = heat_df.mean(axis=0).sort_values(ascending=False).head(12).index.tolist()
    heat_df = heat_df[top_overall]
    fig8 = px.imshow(
        heat_df, text_auto=".2f", aspect="auto", color_continuous_scale="Greens",
        labels=dict(x="Feature", y="Exit Type", color="Importance")
    )
    _layout(fig8, "Chart 8 — Feature Importance by Exit Type", height=460)
else:
    fig8 = go.Figure()
    fig8.add_annotation(text="Not enough data per exit type", showarrow=False, font=dict(color=TXT))
    _layout(fig8, "Chart 8 — Feature Importance by Exit Type (insufficient data)", height=300)

# ─────────────────────────────────────────────────────────────────
# COMBINE INTO SINGLE HTML
# ─────────────────────────────────────────────────────────────────
charts_html = [html_div(f) for f in [fig1, fig2, fig3, fig4, fig5, fig6, fig7, fig8]]
chart_titles = [
    "Chart 1 — Feature Importance",
    "Chart 2 — Score vs PnL",
    "Chart 3 — Score Distribution",
    "Chart 4 — Correlation Heatmap",
    "Chart 5 — Component Contribution",
    "Chart 6 — Exit Reason Performance",
    "Chart 7 — Score Decile Analysis",
    "Chart 8 — Feature Importance per Exit Type",
]

# Top features narrative
top5_perm = imp_df.head(5)["feature"].tolist()
top5_corr = corr_df.head(5).to_dict("records")
score_pnl_r = float(df["sig_score"].corr(df["pnl_usd"]))

dashboard_html = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>KARA Score Audit Dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; padding: 20px; background: {DARK_BG}; color: {TXT}; }}
  h1 {{ color: {GREEN}; margin: 0 0 8px; }}
  h2 {{ color: {YELLOW}; border-bottom: 2px solid {YELLOW}; padding-bottom: 6px; margin-top: 32px; }}
  .summary {{ background: {PANEL}; padding: 18px 22px; border-radius: 10px; margin: 12px 0 24px; border-left: 4px solid {GREEN}; }}
  .summary table {{ width: 100%; border-collapse: collapse; }}
  .summary td {{ padding: 4px 12px; }}
  .summary td:first-child {{ color: #aaa; }}
  .summary td:last-child {{ font-weight: bold; color: {TXT}; text-align: right; }}
  .chart-container {{ margin: 14px 0 28px; padding: 14px; background: {PANEL}; border-radius: 10px; }}
  .findings {{ background: #2a1a1a; border-left: 4px solid {RED}; padding: 18px; border-radius: 10px; margin: 24px 0; }}
  .findings ul {{ margin: 8px 0; padding-left: 24px; }}
  .findings li {{ margin: 6px 0; }}
  .crit {{ color: {RED}; font-weight: bold; }}
  .ok {{ color: {GREEN}; font-weight: bold; }}
  .warn {{ color: {YELLOW}; font-weight: bold; }}
</style>
</head><body>

<h1>🎯 KARA Bot — Score Audit Dashboard</h1>
<div style="color:#aaa">Production data: {summary['total_trades']} trades · {summary['n_features']} features · Generated from Railway DB</div>

<div class="summary">
<table>
<tr><td>Total Trades</td><td>{summary['total_trades']}</td></tr>
<tr><td>Win / Loss</td><td>{summary['wins']} / {summary['losses']}</td></tr>
<tr><td>Win Rate</td><td><span class="{'ok' if summary['win_rate_pct']>=50 else 'warn'}">{summary['win_rate_pct']}%</span></td></tr>
<tr><td>Total PnL</td><td><span class="{'ok' if summary['total_pnl']>0 else 'crit'}">${summary['total_pnl']}</span></td></tr>
<tr><td>Avg Win / Loss</td><td>${summary['avg_win']} / ${summary['avg_loss']}</td></tr>
<tr><td>Profit Factor</td><td><span class="{'ok' if summary['profit_factor']>=1 else 'crit'}">{summary['profit_factor']}</span></td></tr>
<tr><td>Expectancy / trade</td><td><span class="{'ok' if summary['expectancy_per_trade']>0 else 'crit'}">${summary['expectancy_per_trade']}</span></td></tr>
<tr><td>Score ↔ PnL Correlation</td><td><span class="crit">{score_pnl_r:.3f}</span> (≈ random)</td></tr>
<tr><td>Score Decomposition R²</td><td><span class="crit">{summary['lr_score_R2']}</span> (final_score is NOT a clean sum of components!)</td></tr>
<tr><td>RF Win/Loss accuracy</td><td>{summary['rf_test_accuracy']} (baseline {summary['rf_baseline']})</td></tr>
</table>
</div>

<div class="findings">
<b>🔥 CRITICAL FINDINGS (top of mind):</b>
<ul>
<li><span class="crit">All 3 main analyzers (OI+Funding, Liquidation, Orderbook) returned 0 for every trade</span> — zero variance. The "multi-factor scoring" is effectively only session_bonus + raw_score.</li>
<li><span class="crit">Score 73–97 (top decile) has 21% win rate and -$22 PnL</span> — score is INVERSELY predictive at the top.</li>
<li><span class="crit">momentum_exit: 0/9 wins, -$38 (-$4.23/trade)</span> — broken exit logic.</li>
<li><span class="warn">trailing_stop: 14/14 wins, +$19</span> — only working exit but rarely triggers.</li>
<li>Top predictive features are <b>volatility/sizing</b> (entry_atr, realized_vol, leverage), <b>not</b> the signal score components.</li>
</ul>
</div>
"""

for title, body in zip(chart_titles, charts_html):
    dashboard_html += f"""
<h2>{title}</h2>
<div class="chart-container">{body}</div>
"""
dashboard_html += "</body></html>"

with open(f"{OUT}/kara_score_audit_dashboard.html", "w", encoding="utf-8") as f:
    f.write(dashboard_html)

print(f"Dashboard generated: {OUT}/kara_score_audit_dashboard.html")
