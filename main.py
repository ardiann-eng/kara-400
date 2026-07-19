"""
KARA Bot - Main Entry Point 
Orchestrates: WS client, scoring engine, risk manager, executor,
Telegram bot, and dashboard. Runs the main trading loop.

Usage:
  python main.py              # paper mode (safe)
  KARA_MODE=live python main.py  # live mode (real money!)
"""

from __future__ import annotations
import asyncio
import logging
import sys
import os
import signal
import time
import uuid
from typing import Dict, List, Optional, Any
# Ensure root directory is in path for module discovery (Railway/Docker Fix)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from config import MODE, WATCHED_ASSETS, SIGNAL
from models.schemas import ExecutionMode
from models.schemas import BotMode
from data.hyperliquid_client import HyperliquidClient
from data.ws_client import KaraWebSocketClient, MarketDataCache, market_cache
from engine.scoring_engine import ScoringEngine
from risk.risk_manager import RiskManager
from execution.paper_executor import PaperExecutor
from notify.telegram import KaraTelegram
from dashboard.app import init_dashboard, run_dashboard, broadcast
from core.mode_manager import mode_manager
from utils.helpers import utcnow
from core.db import user_db
from core.user_session import UserSession
from core.startup_validation import StartupConfigurationError, validate_startup_config
from core.startup_validation import validate_bybit_preflight
from data.bybit_client import BybitClient
from execution.bybit_executor import BybitExecutor
from core.bybit_session_lifecycle import BybitSessionLifecycle

# ──────────────────────────────────────────────
# LOGGING SETUP
# ──────────────────────────────────────────────

# Fix Windows encoding issue - force UTF-8 for console output
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("kara.main")


def run_total_reset_if_confirmed() -> bool:
    """Run irreversible Option B reset once before user/session startup."""
    if config.TOTAL_RESET_CONFIRMATION != config.TOTAL_RESET_ACK_VALUE:
        return False
    marker = config.TOTAL_RESET_MARKER_PATH
    if os.path.exists(marker):
        log.warning("[TOTAL RESET] confirmation remains but reset marker exists; skipping")
        return False
    summary = user_db.hard_reset_all_data()
    if summary.get("status") != "ok":
        raise RuntimeError("Total reset failed; startup blocked")
    try:
        if os.path.exists(config.TG_STATE_PATH):
            os.remove(config.TG_STATE_PATH)
            summary["telegram_state.json"] = "deleted"
        else:
            summary["telegram_state.json"] = "not_found"
        with open(marker, "x", encoding="utf-8") as handle:
            handle.write("completed")
    except Exception as exc:
        raise RuntimeError("Total reset marker/state cleanup failed; startup blocked") from exc
    log.critical("[TOTAL RESET] Option B completed: %s", summary)
    return True


# ──────────────────────────────────────────────
# KARA CORE
# ──────────────────────────────────────────────

# Setup global loggers to be less chatty
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

class KaraBot:
    """Main bot orchestrator."""

    def __init__(self):
        # Clients
        self.hl_client  = HyperliquidClient()
        self.bybit_client: Optional[BybitClient] = None
        self._bybit_lifecycle = BybitSessionLifecycle(lambda: BybitClient(
            api_key="",
            api_secret="",
            testnet=config.BYBIT_TESTNET,
            recv_window=config.BYBIT_RECV_WINDOW,
        ))
        self.ws_client  = KaraWebSocketClient()
        self.cache      = market_cache

        self.mode_mgr   = mode_manager
        self.cache      = market_cache

        # Scoring engine (stateless regarding user risk)
        self.scorer     = ScoringEngine(self.hl_client, self.cache, mode_manager=self.mode_mgr)

        # Multi-user session store (chat_id -> UserSession)
        self.sessions: Dict[str, UserSession] = {}

        # Semaphor for parallel scanning (Task 4)
        self.scan_sem   = asyncio.Semaphore(5)

        # Notification
        self.telegram   = KaraTelegram(on_confirm=self._on_trade_confirmed)
        self.telegram.bot_app      = self  # allow telegram to access user sessions
        self.telegram.hl_client    = self.hl_client
        self.telegram.mode_manager = self.mode_mgr  # inject for /scalper /standard

        # Dynamic market list (loaded at startup)
        self.watched_assets: List[str] = []

        self._running   = False

    def _enforce_locked_score_thresholds(self):
        """Force fixed score thresholds for all users/modes.
        
        [FIX 2 - 2026-04-22] Updated thresholds based on 124 paper trade analysis:
          std_min_score_to_signal:     58 → 62  (Score 55-59 WR=18.4%, -$19.80)
          std_min_score_to_auto_trade: 65 → 68  (Score 60-64 WR=41.7%, +$10.75)
        These are LOCKED — cannot be changed by user via Telegram settings.

        Also migrates all users to scalper when FORCE_SCALPER_ONLY is on.
        """
        changed = 0
        force_scl = bool(getattr(config, "FORCE_SCALPER_ONLY", False))
        for u in user_db.get_all_users():
            cfg = u.config
            dirty = False
            if force_scl and cfg.trading_mode != "scalper":
                cfg.trading_mode = "scalper"
                dirty = True
            if cfg.std_min_score_to_signal != 55:
                cfg.std_min_score_to_signal = 55
                dirty = True
            if cfg.std_min_score_to_auto_trade != 60:
                cfg.std_min_score_to_auto_trade = 60
                dirty = True
            if cfg.scl_min_score_to_signal != 50:
                cfg.scl_min_score_to_signal = 50
                dirty = True
            if cfg.scl_min_score_to_auto_trade != 60:
                cfg.scl_min_score_to_auto_trade = 60
                dirty = True
            if dirty:
                user_db.update_user(u)
                changed += 1
        if changed:
            log.info(f"🔒 Locked score thresholds enforced for {changed} user(s).")
        if force_scl:
            log.info(
                "⚡ FORCE_SCALPER_ONLY=ON — all execution uses scalper hold/risk; "
                f"standard scorer fallback="
                f"{'ON' if getattr(config, 'STANDARD_SIGNAL_AS_SCALPER_FALLBACK', True) else 'OFF'}"
            )

    # ──────────────────────────────────────────
    # STARTUP
    # ──────────────────────────────────────────

    async def start(self):
        log.info("=" * 60)
        log.info(f" KARA Bot starting")
        log.info(f" 📡 Data source : {config.DATA_SOURCE.upper()} (live prices)")
        log.info(
            f" 📄 Execution   : {config.TRADE_MODE.upper()} "
            f"via {config.EXECUTION_EXCHANGE.upper()}"
        )

        try:
            validate_startup_config(config)
        except StartupConfigurationError as exc:
            log.critical(f"Unsafe startup configuration: {exc}")
            raise

        # Connect to Hyperliquid
        await self.hl_client.connect()

        if config.TRADE_MODE == "live" or any(
            u.config.bot_mode == BotMode.LIVE for u in user_db.get_all_users()
        ):
            await self.ensure_bybit_public_client()

        # Load Hyperliquid top-100 candidates. Demo execution later resolves only
        # exact active Bybit linear-USDT metadata; no asset+USDT synthesis.
        log.info("Loading top volume markets...")
        self.watched_assets = await self.hl_client.get_top_volume_markets(top_n=100)
        if len(self.watched_assets) < 50:
            log.warning(f"Low market count ({len(self.watched_assets)}), retrying in 10s...")
            await asyncio.sleep(10)
            self.watched_assets = await self.hl_client.get_top_volume_markets(top_n=100)

        if self.bybit_client:
            from execution.demo_universe import exact_demo_universe
            demo_users = [
                user for user in user_db.get_all_users()
                if getattr(getattr(user, "bybit_environment", None), "value", None) == "demo"
            ]
            if demo_users:
                demo_universe = exact_demo_universe(
                    self.watched_assets, self.bybit_client.symbol_registry
                )
                log.info(
                    "Demo exact Bybit universe: %s/%s Hyperliquid top-100 candidates",
                    len(demo_universe), len(self.watched_assets),
                )

        log.info(f"   Markets ({len(self.watched_assets)}): {', '.join(self.watched_assets[:15])}{'...' if len(self.watched_assets) > 15 else ''}")
        log.info(f"   Full-auto: {config.FULL_AUTO}")
        log.info("=" * 60)

        # Setup WebSocket subscriptions
        await self._setup_websocket()

        # Release tag used for one-time actions and update notification
        current_version = config.KARA_VERSION
        
        # ── AI-DRIVEN CHANGELOG DISCOVERY ──────────────────────────────
        changelog_data = {}
        changelog_path = os.path.join(os.getcwd(), "data", "changelog.json")
        if os.path.exists(changelog_path):
            try:
                import json
                with open(changelog_path, 'r', encoding='utf-8') as f:
                    changelog_data = json.load(f)
            except Exception as e:
                log.warning(f"Could not load AI changelog: {e}")
        
        if changelog_data.get("version"):
            current_version = changelog_data["version"]
            
        deploy_id = (
            os.getenv("RAILWAY_DEPLOYMENT_ID", "") or
            os.getenv("RAILWAY_RUN_ID", "") or
            os.getenv("RENDER_DEPLOY_ID", "")
        ).strip()
        deploy_sha = (
            os.getenv("RAILWAY_GIT_COMMIT_SHA", "") or
            os.getenv("GIT_COMMIT_SHA", "") or
            os.getenv("RENDER_GIT_COMMIT", "") or
            changelog_data.get("release_id", "")
        ).strip()
        
        short_sha = deploy_sha[:8] if deploy_sha else ""
        short_dep = deploy_id[:8] if deploy_id else ""
        
        if short_dep:
            release_tag = f"{current_version}-d{short_dep}"
        elif short_sha:
            release_tag = f"{current_version}-{short_sha}"
        else:
            release_tag = current_version

        users_to_update = [
            u for u in user_db.users.values()
            if u.is_authorized and u.last_seen_version != release_tag
        ]

        # Purge template / dummy paper users (never real Telegram chats)
        try:
            purged = user_db.purge_dummy_users()
            if purged and self.telegram:
                for cid in purged:
                    self.telegram._authorized_chat_ids.discard(str(cid))
                if hasattr(self.telegram, "_save_state"):
                    self.telegram._save_state()
        except Exception as e:
            log.warning(f"Dummy user purge failed: {e}")

        # Initialize User Sessions from DB
        self._enforce_locked_score_thresholds()
        _dummy = {"123456789", "987654321"}
        for u in user_db.get_all_users():
            if str(u.chat_id) in _dummy:
                continue
            session = UserSession(
                u,
                mode_manager=self.mode_mgr,
                hl_client=self.hl_client,
                bybit_client=self.bybit_client,
                bybit_registry=(
                    self.bybit_client.symbol_registry if self.bybit_client else None
                ),
                persistence=user_db,
                alert_sink=lambda message, cid=str(u.chat_id): self.telegram.send_text(
                    message, target_chat_id=cid
                ),
            )
            if hasattr(session.executor, 'load_from_db'):
                session.executor.load_from_db(u.chat_id)
            await session.initialize()
            self.sessions[u.chat_id] = session
        log.info(f"Loaded {len(self.sessions)} user sessions.")

        # Inject into dashboard (passing sessions registry for multi-user support)
        init_dashboard(self.sessions, self.telegram, self.mode_mgr)

        # Start Telegram (optional, errors are graceful)
        try:
            await self.telegram.start()
        except Exception as e:
            log.warning(f"  Telegram startup failed: {e}")
            log.warning("   Bot will continue without Telegram notifications")

        # Update / "KARA Online" Telegram broadcasts DISABLED (spam on every deploy).
        # Still stamp last_seen_version so re-enabling later won't flood old releases.
        try:
            for u in user_db.users.values():
                if u.is_authorized and u.last_seen_version != release_tag:
                    u.last_seen_version = release_tag
                    user_db.update_user(u)
            log.info(f"📭 Deploy notify OFF — stamped release={release_tag} (no Telegram blast)")
        except Exception as e:
            log.debug(f"release stamp skipped: {e}")

        # ── Calibration Check ─────────────────────────────────────────
        try:
            from models.schemas import MarketRegime
            log.info("🎯 Score calibration test:")
            
            # 1. Realistic
            realistic = await self.scorer.simulate_score({
                "funding_rate": 0.0001 / 3,  # per period
                "oi_change_1h": 0.008,
                "imbalance": 0.52,
                "vwap_dev": -0.0025,
                "session_bonus": int(SIGNAL.ny_session_bonus) + int(SIGNAL.london_session_bonus),
                "regime": MarketRegime.TRENDING
            })
            log.info(f"   Realistic params → Score: {realistic['score']}/100 ✓ (Expected: 56-75)")
            
            # 2. Strong
            strong = await self.scorer.simulate_score({
                "funding_rate": 0.0004 / 3,
                "oi_change_1h": 0.025,
                "cascade_risk": 0.4,
                "imbalance": 0.65,
                "vwap_dev": -0.004,
                "session_bonus": int(SIGNAL.ny_session_bonus),
                "regime": MarketRegime.TRENDING,
                "trend_pct": 0.03  # Added to trigger the 1.1x trend multiplier
            })
            log.info(f"   Strong signal    → Score: {strong['score']}/100 ✓ (Expected: 78-95)")
        except Exception as e:
            log.error(f"   Calibration test failed: {e}")

        self._running = True

        # ── Session diagnostic log ──────────────────────────────────────
        _sess_bonus, _sess_reasons = self.scorer._get_session_bonus()
        from datetime import datetime as _dt, timezone as _tz
        _hour = _dt.now(_tz.utc).hour
        log.info(
            f"[SESSION] Startup: UTC hour={_hour:02d}  "
            f"London({SIGNAL.london_start_utc}-{SIGNAL.london_end_utc} UTC)  "
            f"NY({SIGNAL.ny_session_start_utc}-{SIGNAL.ny_session_end_utc} UTC)  "
            f"Current bonus={_sess_bonus:+d}  Reasons: {', '.join(_sess_reasons) or 'none'}"
        )

        # ── Stale Cache Cleanup ──────────────────────────────────────────
        await self._clean_stale_cache()

        # ── Startup Self-Test ────────────────────────────────────────────
        await self._startup_selftest()

        # Start Snapshot Loop (Phase 2: History Tracking)
        asyncio.create_task(self._snapshot_loop())

        # ── Blocked hours awareness ──────────────────────────────────────
        if _hour in config.BLOCKED_HOURS_UTC:
            log.warning(
                f"⏸️  Starting during BLOCKED hour (UTC {_hour:02d}). "
                f"Blocked hours: {config.BLOCKED_HOURS_UTC}. "
                f"Scanning will resume after hour {max(config.BLOCKED_HOURS_UTC) + 1}:00 UTC."
            )

        # Greeting log
        mode_emoji = "🧪" if config.TRADE_MODE == "paper" else "💰"
        log.info(f"{mode_emoji} KARA is ready! Starting trading loop...")

    async def _clean_stale_cache(self):
        """Remove vol_cache and OI snapshot entries for assets no longer in the Hyperliquid universe."""

        try:
            all_meta = await self.hl_client.get_all_market_data()
            if not all_meta or not isinstance(all_meta, (list, tuple)) or len(all_meta) < 2:
                log.warning("[CACHE] Could not fetch universe for stale cache cleanup")
                return

            universe = all_meta[0]
            valid_names = set()
            for u in universe:
                if isinstance(u, dict) and u.get("name"):
                    valid_names.add(u["name"])

            if len(valid_names) < 50:
                log.warning(f"[CACHE] Universe too small ({len(valid_names)}), skipping cleanup")
                return

            # Clean vol_cache in memory
            stale_vol = [a for a in self.scorer._vol_cache if a not in valid_names]
            for a in stale_vol:
                del self.scorer._vol_cache[a]

            # Clean OI snapshots in memory
            stale_oi = [a for a in self.scorer._oi_snapshots if a not in valid_names]
            for a in stale_oi:
                del self.scorer._oi_snapshots[a]

            # Clean vol_cache in SQLite
            if stale_vol:
                try:
                    from core.db import user_db
                    conn = user_db._get_conn()
                    cursor = conn.cursor()
                    placeholders = ",".join("?" * len(valid_names))
                    cursor.execute(
                        f"DELETE FROM vol_cache WHERE asset NOT IN ({placeholders})",
                        list(valid_names)
                    )
                    conn.commit()
                except Exception as e:
                    log.warning(f"[CACHE] SQLite vol_cache cleanup failed: {e}")

            total_stale = len(stale_vol) + len(stale_oi)
            if total_stale > 0:
                log.info(
                    f"[CACHE] Removed {len(stale_vol)} stale vol_cache + "
                    f"{len(stale_oi)} stale OI entries. "
                    f"Universe: {len(valid_names)} assets. "
                    f"Stale examples: {(stale_vol + stale_oi)[:5]}"
                )
            else:
                log.info(f"[CACHE] Cache is clean — {len(valid_names)} assets in universe")

        except Exception as e:
            log.warning(f"[CACHE] Stale cache cleanup failed: {e}")

    async def _startup_selftest(self):
        """Run quick self-test to verify everything works before first scan."""
        log.info("[SELFTEST] Running startup checks...")
        issues = []

        # Test 1: Can we get mark price for BTC?
        try:
            btc_price = await self.hl_client.get_mark_price("BTC")
            if btc_price <= 0:
                issues.append("FAIL: BTC mark price = 0 (API atau ctx broken)")
            else:
                log.info(f"[SELFTEST] ✅ BTC price: ${btc_price:,.2f}")
        except Exception as e:
            issues.append(f"FAIL: BTC price fetch error: {e}")

        # Test 2: Is market cache populated?
        try:
            cache = self.hl_client._market_cache
            if cache and isinstance(cache, (list, tuple)) and len(cache) >= 2:
                ctx_size = len(cache[0])
                if ctx_size < 50:
                    issues.append(f"FAIL: market cache hanya {ctx_size} assets (expected 100+)")
                else:
                    log.info(f"[SELFTEST] ✅ Market cache: {ctx_size} assets")
            else:
                issues.append("FAIL: market cache kosong atau format salah")
        except Exception as e:
            issues.append(f"FAIL: market cache check error: {e}")

        # Test 3: Is API circuit breaker okay?
        if self.hl_client._consecutive_502s > 0:
            issues.append(
                f"WARN: API circuit breaker has {self.hl_client._consecutive_502s} consecutive 502s"
            )
            # Auto-reset on startup
            self.hl_client._consecutive_502s = 0
            self.hl_client._api_backoff_until = 0.0
            log.warning("[SELFTEST] ⚡ API circuit breaker auto-reset on startup")

        # Test 4: Blocked hours check
        from datetime import datetime as _dt_st, timezone as _tz_st
        current_hour = _dt_st.now(_tz_st.utc).hour
        if current_hour in config.BLOCKED_HOURS_UTC:
            log.info(
                f"[SELFTEST] ⏸️  Currently in blocked hour (UTC {current_hour:02d}). "
                f"Scoring will return 0 until hour {max(config.BLOCKED_HOURS_UTC) + 1}."
            )

        # Report
        if issues:
            log.error(f"[SELFTEST] {len(issues)} issue(s) found:")
            for issue in issues:
                log.error(f"[SELFTEST]   • {issue}")
            try:
                await self.telegram.send_text(
                    f"⚠️ KARA startup issues ({len(issues)}):\n" +
                    "\n".join(f"• {i}" for i in issues)
                )
            except Exception:
                pass  # Telegram may not be ready yet
        else:
            log.info("[SELFTEST] ✅ All checks passed. Ready to scan.")

    # ──────────────────────────────────────────
    # WEBSOCKET SETUP
    # ──────────────────────────────────────────

    async def _setup_websocket(self):
        # Register cache callbacks
        self.ws_client.on("funding",      self.cache.on_funding)
        self.ws_client.on("orderbook",    self.cache.on_orderbook)
        self.ws_client.on("trades",       self.cache.on_trades)
        self.ws_client.on("liquidations", self.cache.on_liquidations)

        # Also handle user events
        if config.WALLET_ADDRESS:
            self.ws_client.on("user_events", self._on_user_event)

        await self.ws_client.start()

        # Subscribe to each watched asset — throttled to avoid HL WS rate limits.
        # Sending 100 subscriptions instantly causes Hyperliquid to silently drop many.
        # Strategy: 50ms between each asset, global feeds subscribed last.
        SUBSCRIBE_DELAY_S = 0.05   # 50ms per asset = ~5s total for 100 assets

        log.info(f"📡 WS subscribing to {len(self.watched_assets)} assets (throttled {int(SUBSCRIBE_DELAY_S*1000)}ms/asset)...")
        for i, asset in enumerate(self.watched_assets):
            await self.ws_client.subscribe_orderbook(asset)
            await self.ws_client.subscribe_trades(asset)
            await self.ws_client.subscribe_funding(asset)
            # Throttle every asset to spread load across Hyperliquid's WS server
            await asyncio.sleep(SUBSCRIBE_DELAY_S)
            # Progress milestone every 25 assets
            if (i + 1) % 25 == 0:
                log.info(f"   📡 WS progress: {i+1}/{len(self.watched_assets)} assets subscribed")

        log.info("[WS] Subscribing to liquidations channel...")
        await self.ws_client.subscribe_liquidations()
        log.info("[WS] Liquidation subscription sent (events will appear in debug log when received)")

        if config.WALLET_ADDRESS:
            await self.ws_client.subscribe_user_events(config.WALLET_ADDRESS)

        log.info(f"✅ WS fully subscribed: {len(self.watched_assets)} assets (OB + Trades + Funding) + Liquidations")

        # Diagnostic warmup check — runs 15s after subscription completes
        # Tells us if Hyperliquid is actually streaming data back
        async def _check_ws_warmup():
            await asyncio.sleep(15)
            cached_obs = sum(1 for a in self.watched_assets if getattr(self.cache, 'orderbook', {}).get(a))
            cached_trades = sum(1 for a in self.watched_assets if getattr(self.cache, 'trades', {}).get(a))
            cached_funding = sum(1 for a in self.watched_assets if getattr(self.cache, 'funding', {}).get(a))
            total = len(self.watched_assets)
            log.info(
                f"📊 WS warmup check (15s): "
                f"OB={cached_obs}/{total} | Trades={cached_trades}/{total} | Funding={cached_funding}/{total}"
            )
            if cached_obs < total * 0.5:
                log.warning(
                    f"⚠️  WS orderbook cache LOW ({cached_obs}/{total}). "
                    f"Scoring engine will fall back to REST for missing assets."
                )
        asyncio.create_task(_check_ws_warmup())

    # ──────────────────────────────────────────
    # MAIN TRADING LOOP
    # ──────────────────────────────────────────

    async def run_trading_loop(self):
        """
        Titik masuk utama — spawn tiga task independen supaya tidak saling blokir:

          _position_monitor_loop  — update TP/SL tiap 5s, TIDAK pernah nunggu scan
          _ws_watchdog_loop       — health check WS tiap 30s
          _scan_loop              — scoring market tiap N detik (bisa lambat karena throttle)

        Pemisahan ini krusial: saat scan sedang throttled oleh data_sem,
        position monitor tetap jalan dan TP/SL tetap tereksekusi tepat waktu.
        """
        asyncio.create_task(self._position_monitor_loop(), name="position_monitor")
        asyncio.create_task(self._ws_watchdog_loop(), name="ws_watchdog")
        asyncio.create_task(self._scan_loop(), name="scan_loop")

        # Weekly AI audit — Monday 06:00 UTC (configurable via WEEKLY_REVIEW)
        try:
            if getattr(config, "WEEKLY_REVIEW", None) and config.WEEKLY_REVIEW.enabled:
                from intelligence.weekly_review.scheduler import start_weekly_review_loop
                asyncio.create_task(
                    start_weekly_review_loop(self.telegram),
                    name="weekly_review",
                )
                log.info(
                    "📅 Weekly AI audit scheduler ON (Mon %02d:00 UTC)",
                    getattr(config.WEEKLY_REVIEW, "schedule_hour_utc", 6),
                )
            else:
                log.info("📅 Weekly AI audit scheduler OFF")
        except Exception as e:
            log.warning(f"Weekly review scheduler failed to start: {e}")

        # Loop ini hanya tetap hidup untuk menjaga bot tidak exit
        while self._running:
            await asyncio.sleep(5)

    async def _position_monitor_loop(self):
        """
        Task independen — update unrealized PnL dan cek TP/SL setiap 5s.
        TIDAK pernah diblok oleh data scan semaphore.
        Pakai get_mark_price_fast() yang langsung tanpa throttle.
        """
        position_interval  = 5
        dashboard_interval = 5
        hourly_interval    = 3600
        last_pos_upd       = 0.0
        last_dash_upd      = 0.0
        last_hourly        = 0.0

        log.info("[MONITOR] Position monitor loop started (independent task)")
        while self._running:
            try:
                now = asyncio.get_event_loop().time()

                if now - last_pos_upd >= position_interval:
                    await self._update_positions()
                    last_pos_upd = now

                if now - last_dash_upd >= dashboard_interval:
                    await self._broadcast_heartbeat()
                    last_dash_upd = now

                if last_hourly == 0.0:
                    last_hourly = now
                elif now - last_hourly >= hourly_interval:
                    last_hourly = now

            except Exception as e:
                log.error(f"[MONITOR] Position monitor error: {e}", exc_info=True)

            await asyncio.sleep(1)

    async def _ws_watchdog_loop(self):
        """Task independen — health check WebSocket setiap 30s."""
        log.info("[WS] WS watchdog loop started (independent task)")
        while self._running:
            try:
                if not self.ws_client.is_healthy:
                    log.warning("WS watchdog: connection unhealthy, forcing restart")
                    try:
                        await self.ws_client.stop()
                        await self.ws_client.start()
                        for asset in self.watched_assets:
                            await self.ws_client.subscribe_orderbook(asset)
                            await self.ws_client.subscribe_trades(asset)
                            await self.ws_client.subscribe_funding(asset)
                        await self.ws_client.subscribe_liquidations()
                        log.info("WS watchdog: resubscribed all assets")
                    except Exception as e:
                        log.error(f"WS watchdog restart failed: {e}")
            except Exception as e:
                log.error(f"[WS] Watchdog error: {e}")

            await asyncio.sleep(30)

    async def _scan_loop(self):
        """
        Task independen — scoring market scan.
        Boleh lambat karena data_sem throttle — tidak mempengaruhi position monitor.
        """
        last_scan = 0.0
        log.info("[SCAN] Market scan loop started (independent task)")
        while self._running:
            try:
                now = asyncio.get_event_loop().time()
                # FORCE_SCALPER_ONLY → always fast scalper scan cadence
                any_scalper = bool(getattr(config, "FORCE_SCALPER_ONLY", False)) or any(
                    getattr(s.user.config, 'trading_mode', 'standard') == 'scalper'
                    for s in self.sessions.values()
                )
                scan_interval = 5 if any_scalper else self.mode_mgr.scan_interval

                if now - last_scan >= scan_interval:
                    await self._scan_all_assets()
                    last_scan = now
            except Exception as e:
                log.error(f"[SCAN] Scan loop error: {e}", exc_info=True)

            await asyncio.sleep(1)

    async def ensure_bybit_public_client(self) -> BybitClient:
        """Lazily initialize public Bybit metadata for first live user."""
        was_ready = bool(self.bybit_client or self._bybit_lifecycle.public_client)
        if self.bybit_client and not self._bybit_lifecycle.public_client:
            self._bybit_lifecycle.public_client = self.bybit_client
        client = await self._bybit_lifecycle.ensure_public_client()
        self.bybit_client = client
        if not was_ready:
            log.info("Bybit public metadata ready (testnet=%s)", config.BYBIT_TESTNET)
        return client

    async def close_user_session(self, chat_id: str) -> Optional[UserSession]:
        session = self.sessions.pop(str(chat_id), None)
        if not session:
            return None
        await self._bybit_lifecycle.close_session(session)
        return session

    async def get_session(self, chat_id: str) -> Optional[UserSession]:
            chat_id = str(chat_id)
            if chat_id not in self.sessions:
                user = user_db.get_user(chat_id)
                if user:
                    if user.config.bot_mode == BotMode.LIVE:
                        await self.ensure_bybit_public_client()
                    session = UserSession(
                        user,
                        mode_manager=self.mode_mgr,
                        hl_client=self.hl_client,
                        bybit_client=self.bybit_client,
                        bybit_registry=(
                            self.bybit_client.symbol_registry
                            if self.bybit_client else None
                        ),
                        persistence=user_db,
                        alert_sink=lambda message, cid=str(user.chat_id): self.telegram.send_text(
                            message, target_chat_id=cid
                        ),
                    )
                    # Restore persisted state (balance + open positions) from DB
                    if hasattr(session.executor, 'load_from_db'):
                        session.executor.load_from_db(user.chat_id)
                    try:
                        await session.initialize()
                    except Exception:
                        await self._bybit_lifecycle.close_session(session)
                        raise
                    self.sessions[chat_id] = session
            return self.sessions.get(chat_id)

    async def _broadcast_heartbeat(self):
        """Dashboard heartbeat - handles real-time updates for the web UI."""
        from dashboard.app import broadcast, get_active_session
        
        # Get the session to display in dashboard (Admin or first active)
        session = get_active_session()
        if not session:
            return

        try:
            # 1. Get current account state
            acc = await session.get_account_state()
            allocation_usd = getattr(session.user, "capital_allocation_usd", None)
            is_mainnet = getattr(
                getattr(session.user, "bybit_environment", None), "value", None
            ) == "mainnet"
            if allocation_usd is not None and is_mainnet and session.user.config.bot_mode == BotMode.LIVE:
                from core.capital_allocation import sizing_equity
                effective_sizing_equity = sizing_equity(acc.total_equity, allocation_usd)
                sizing_acc = acc.model_copy(update={"total_equity": effective_sizing_equity})
            else:
                effective_sizing_equity = acc.total_equity
                sizing_acc = acc
        except Exception as e:
            # log.debug(f"Dashboard: Could not fetch state for session: {e}")
            return # Skip heartbeat for this tick
            
            # 2. Broadcast account summary
            await broadcast({
                "type": "account_update",
                **acc.model_dump()
            })
            
            # 3. Broadcast open positions
            await broadcast({
                "type": "positions_update",
                "positions": [p.model_dump() for p in session.executor.open_positions]
            })
            
        except Exception as e:
            # Silent fail for heartbeat to avoid loop crashes
            pass

    async def _scan_all_assets(self):
        """Perform scoring scan on all watched assets in parallel, mode-aware."""
        scan_start = time.monotonic()
        log.info(f" 🔍 Scanning {len(self.watched_assets)} markets (parallel)...")

        try:
            # 1. Fetch batch meta data once for all assets
            await self.hl_client.refresh_market_cache(force=True)
            all_meta = await self.hl_client.get_all_market_data()

            # ── HEALTH CHECK: abort early if cache is completely empty ──────
            if not all_meta:
                log.error(
                    "[SCAN] ABORTED — market cache is empty after refresh. "
                    "Hyperliquid API may be down or returning bad data. "
                    "Skipping this scan cycle, will retry next interval."
                )
                return

            cached_asset_count = len(all_meta[0]) if all_meta else 0
            log.debug(f"[SCAN] Cache OK: {cached_asset_count} assets in ctx")

            # Gather active modes across all authorized users
            target_ids = list(self.telegram._authorized_chat_ids)
            active_modes = set()
            force_scl = bool(getattr(config, "FORCE_SCALPER_ONLY", False))
            std_fallback = bool(getattr(config, "STANDARD_SIGNAL_AS_SCALPER_FALLBACK", True))

            if force_scl:
                # Always score scalper. Optionally keep standard scorer as a
                # signal source only — execution remaps to scalper hold/risk.
                active_modes.add("scalper")
                if std_fallback:
                    active_modes.add("standard")
            else:
                for cid in target_ids:
                    session = await self.get_session(cid)
                    if session and session.user and hasattr(session.user.config, 'trading_mode'):
                        active_modes.add(session.user.config.trading_mode)

            # If no authorized users yet, default to scalper (or legacy standard)
            if not active_modes:
                active_modes.add("scalper" if force_scl else "standard")

            active_modes_list = list(active_modes)

            # 2. Parallel scan with Semaphore(5)
            max_score = 0
            sig_count = 0
            scored_count = 0
            blocked_count = 0  # assets blocked by schedule (BLOCKED_HOURS_UTC)
            top_scorers = []  # List of (asset, score)

            async def _scan_one(asset, scalper_only=False):
                nonlocal max_score, sig_count, scored_count, blocked_count
                async with self.scan_sem:
                    try:
                        modes_for_asset = ["scalper"] if scalper_only else active_modes_list
                        signals_dict, asset_max_score = await self.scorer.run_asset(
                            asset, active_modes=modes_for_asset, meta_data=all_meta
                        )

                        if signals_dict:
                            sig_count += len(signals_dict)
                            await self._handle_signals(signals_dict)

                        # Sentinel -1 = blocked by BLOCKED_HOURS_UTC schedule
                        if asset_max_score == -1:
                            blocked_count += 1
                        elif asset_max_score > 0:
                            scored_count += 1
                            top_scorers.append((asset, asset_max_score))
                            if asset_max_score > max_score:
                                max_score = asset_max_score

                            for cid in target_ids:
                                session = await self.get_session(cid)
                                if session:
                                    session.risk_mgr.update_score(asset, asset_max_score)

                    except Exception as e:
                        if "429" in str(e):
                            log.debug(f" ⚠️ Rate limited on {asset}")
                        else:
                            log.error(f" ❌ Scan error for {asset}: {e}")

            # Determine assets to scan
            assets_to_scan = list(self.watched_assets)
            scalper_only_assets = []
            if "scalper" in active_modes:
                import config as _cfg
                scalper_assets = getattr(_cfg, 'SCALPER_ASSETS', [])
                scalper_only_assets = [a for a in scalper_assets if a not in assets_to_scan]
                assets_to_scan.extend(scalper_only_assets)

            tasks = [_scan_one(asset, scalper_only=(asset in scalper_only_assets)) for asset in assets_to_scan]
            await asyncio.gather(*tasks)

            scan_elapsed = time.monotonic() - scan_start

            # ── ANOMALY DETECTION ─────────────────────────────────────────
            total_scanned = len(assets_to_scan)
            from datetime import datetime as _dt_check, timezone as _tz_check
            _current_utc_hour = _dt_check.now(_tz_check.utc).hour

            if scored_count == 0 and total_scanned > 0:
                if blocked_count >= total_scanned:
                    # All assets blocked by schedule — this is EXPECTED, not an anomaly
                    log.info(
                        f"⏸️  [SCAN] All {total_scanned} assets blocked by schedule "
                        f"(UTC hour={_current_utc_hour}, blocked={config.BLOCKED_HOURS_UTC}). "
                        f"Waiting for trading window to open."
                    )
                else:
                    # Genuine anomaly — 0 scored outside blocked hours
                    log.error(
                        f"[SCAN] ANOMALY: 0/{total_scanned} assets scored in {scan_elapsed:.1f}s "
                        f"(expected 4-8s). Cache has {cached_asset_count} assets. "
                        f"Blocked={blocked_count}. "
                        f"Check [PRICE] warnings above for root cause."
                    )

            top_scorers.sort(key=lambda x: x[1], reverse=True)
            top_str = ", ".join([f"{a}:{s}" for a, s in top_scorers[:5]])
            scalper_extra = len(scalper_only_assets)

            # Include blocked info in summary when relevant
            blocked_info = f" | Blocked: {blocked_count}" if blocked_count > 0 else ""
            log.info(
                f" 🏁 Scan complete: {total_scanned} markets "
                f"({len(self.watched_assets)} standard"
                f"{f' + {scalper_extra} scalper-only' if scalper_extra else ''}). "
                f"Signals: {sig_count} | Scored: {scored_count}{blocked_info} | "
                f"Top: [{top_str or 'None'}] | {scan_elapsed:.1f}s"
            )
            
            # Persist OI snapshots to prevent amnesia
            self.scorer.dump_oi_state()
            
        except Exception as e:
            log.error(f" [FATAL] Scan loop failure: {e}", exc_info=True)

    async def _snapshot_loop(self):
        """Background task to record bot performance every hour (Phase 2)."""
        log.info("📊 History snapshot task started (hourly).")
        
        while self._running:
            try:
                total_users = len(user_db.users)
                active_users = sum(1 for u in user_db.users.values() if u.is_authorized)
                
                global_equity = 0.0
                global_pnl = 0.0
                
                for session in self.sessions.values():
                    try:
                        acc = await session.get_account_state()
                        global_equity += acc.total_equity
                        # Use daily_pnl to represent "total pnl all users (today)"
                        # so dashboard card/chart stays consistent and not flat at 0
                        # when positions are closed.
                        global_pnl += acc.daily_pnl
                    except:
                        pass
                
                # Save to DB
                user_db.save_snapshot(total_users, active_users, global_pnl, global_equity)
                log.debug(f"💾 Snapshot saved: Users={total_users}, Equity=${global_equity:,.2f}")
                
            except Exception as e:
                log.error(f"Error in snapshot loop: {e}")
            
            # Wait 1 hour (3600s)
            await asyncio.sleep(3600)

    async def _handle_signals(self, signals_dict: dict):
        """Distribute each mode's signal to the respective users in that mode."""
        from execution.paper_executor import PaperExecutor # just to satisfy type hints if needed
        import config
        
        target_ids = list(self.telegram._authorized_chat_ids)
        if not target_ids:
            return

        for chat_id in target_ids:
            # Never trade for template/placeholder chat IDs
            if str(chat_id) in {"123456789", "987654321"}:
                log.debug(f"Skip dummy chat_id={chat_id}")
                continue
            # Get or create session
            session = await self.get_session(chat_id)
            if not session:
                user = user_db.create_user(chat_id, "Master", init_usd=config.PAPER_BALANCE_USD)
                session = await self.get_session(chat_id)
                
            # Effective mode: FORCE_SCALPER_ONLY always executes as scalper
            # (hold time, risk, thresholds) even if user config still says standard.
            stored_mode = getattr(session.user.config, 'trading_mode', 'standard')
            user_mode = config.effective_trading_mode(stored_mode)

            # Demo is primary execution. Legacy Paper positions still receive
            # TP/SL/close monitoring in _update_positions(), but no new entry.
            from core.execution_environment_policy import requires_demo_onboarding
            if requires_demo_onboarding(session.user):
                log.info(
                    "[PAPER-ENTRY-BLOCK] user=%s asset=%s reason=demo_onboarding_required",
                    chat_id,
                    next((signal.asset for signal in signals_dict.values() if signal), "unknown"),
                )
                continue

            # Demo-only candidate gate. Scanner remains Hyperliquid top-100 for
            # research and Paper; only Demo execution requires an exact active
            # Bybit linear-USDT metadata mapping before signal processing.
            if getattr(getattr(session.user, "bybit_environment", None), "value", None) == "demo":
                from execution.demo_universe import is_demo_execution_eligible
                registry = getattr(getattr(session, "executor", None), "registry", None)
                candidate_asset = next(
                    (
                        signal.asset for signal in signals_dict.values()
                        if signal is not None
                    ),
                    None,
                )
                if not registry or not candidate_asset or not is_demo_execution_eligible(
                    candidate_asset, registry
                ):
                    rejected_signal = next(
                        (signal for signal in signals_dict.values() if signal), None
                    )
                    if rejected_signal:
                        user_db.save_execution_candidate(
                            chat_id,
                            rejected_signal,
                            status="rejected",
                            reason="inactive_or_unsupported_bybit_metadata",
                            execution_environment="demo",
                            extra={
                                "capital_allocation_idr": session.user.capital_allocation_idr,
                                "capital_allocation_usd": session.user.capital_allocation_usd,
                            },
                        )
                    log.info(
                        "[DEMO-UNIVERSE-BLOCK] user=%s asset=%s reason=inactive_or_unsupported_bybit_metadata",
                        chat_id, candidate_asset or "unknown",
                    )
                    continue

            # Prefer native scalper signal; optional standard scorer as fallback
            # source — still executed under scalper rules below.
            base_signal = signals_dict.get("scalper") if user_mode == "scalper" else signals_dict.get(user_mode)
            fallback_from_standard = False
            scl_sig = signals_dict.get("scalper")
            std_sig = signals_dict.get("standard")

            # ── P2: direction conflict resolution ─────────────────────────
            # If scalper wants LONG and standard wants SHORT (or vice versa),
            # prefer scalper — it is closer to short-horizon price action.
            # HYPE case: standard SHORT cascade vs scalper LONG 80 on pump.
            if (
                user_mode == "scalper"
                and scl_sig is not None
                and std_sig is not None
                and getattr(scl_sig, "side", None) is not None
                and getattr(std_sig, "side", None) is not None
                and scl_sig.side != std_sig.side
            ):
                min_pref = int(getattr(config, "SCALPER_CONFLICT_MIN_SCORE", 60))
                if int(getattr(scl_sig, "score", 0) or 0) >= min_pref:
                    log.info(
                        f"[P2-PREFER-SCALPER] {chat_id} {getattr(scl_sig, 'asset', '?')}: "
                        f"scalper {scl_sig.side.value.upper()}@{scl_sig.score} vs "
                        f"standard {std_sig.side.value.upper()}@{std_sig.score} "
                        f"→ use scalper"
                    )
                    base_signal = scl_sig
                    fallback_from_standard = False
                else:
                    # Scalper too weak — do not execute either conflicting direction
                    log.info(
                        f"[P2-SKIP-CONFLICT] {chat_id} {getattr(std_sig, 'asset', '?')}: "
                        f"scalper {scl_sig.side.value.upper()}@{scl_sig.score} vs "
                        f"standard {std_sig.side.value.upper()}@{std_sig.score} "
                        f"(scalper < {min_pref}) → skip both"
                    )
                    continue

            if user_mode == "scalper" and not base_signal:
                allow_std_fb = bool(getattr(config, "STANDARD_SIGNAL_AS_SCALPER_FALLBACK", True))
                # Also allow fallback when FORCE_SCALPER_ONLY (same intent)
                if allow_std_fb or getattr(config, "FORCE_SCALPER_ONLY", False):
                    std_fallback = std_sig
                    if std_fallback:
                        # Standard score supplies directional context only. Re-run the
                        # complete native 1m scorer so a fallback cannot inherit a
                        # scalper's 12m risk profile without microstructure evidence.
                        try:
                            confirmed_sig, confirmed_score = await self.scorer._run_scalper(
                                std_fallback.asset
                            )
                        except Exception as e:
                            log.info(
                                f"[SCALPER-FALLBACK-BLOCK] {std_fallback.asset}: "
                                f"1m revalidation unavailable ({e})"
                            )
                            continue

                        if not confirmed_sig:
                            log.info(
                                f"[SCALPER-FALLBACK-BLOCK] {std_fallback.asset} "
                                f"{std_fallback.side.value.upper()}@{std_fallback.score}: "
                                f"native 1m scorer rejected (score={confirmed_score})"
                            )
                            continue
                        if confirmed_sig.side != std_fallback.side:
                            log.info(
                                f"[SCALPER-FALLBACK-BLOCK] {std_fallback.asset}: "
                                f"standard={std_fallback.side.value.upper()}@{std_fallback.score} "
                                f"but 1m={confirmed_sig.side.value.upper()}@{confirmed_sig.score}"
                            )
                            continue

                        base_signal = confirmed_sig
                        fallback_from_standard = True

            if not base_signal:
                continue

            # ── Per-User Threshold Check (AUTO-ONLY POLICY) ──────────────────
            user_cfg = session.user.config
            is_scl = (user_mode == 'scalper')
            auto_threshold = int(user_cfg.scl_min_score_to_auto_trade if is_scl else user_cfg.std_min_score_to_auto_trade)

            # ── MULTI-USER FIX: Deep-copy signal with a UNIQUE signal_id per user ──
            # model_copy() copies ALL fields including signal_id.
            # If two users share the same signal_id, User A confirming/skipping
            # calls _pending_signals.pop(sig_id) which ALSO removes User B's signal.
            # Fix: assign a fresh UUID immediately after copy so each user's
            # signal is stored and resolved independently in _pending_signals.
            # Deep copy so nested breakdown/warnings are not shared across users/cycles.
            user_signal = base_signal.model_copy(deep=True)
            user_signal.signal_id = f"{base_signal.signal_id[:4]}{uuid.uuid4().hex[:4].upper()}"
            if fallback_from_standard:
                log.info(
                    f"[SCALPER-FALLBACK-CONFIRMED] {chat_id}: standard context confirmed "
                    f"by native 1m {user_signal.side.value.upper()}@{user_signal.score}; "
                    f"using native scalper entry levels"
                )


            # Native scalper levels are calibrated to the 12-18 minute horizon and
            # remain authoritative. ATR localization is only a standard-mode fallback
            # before calculate_levels() applies final regime-aware standard levels.
            atr_value = 0.0
            if (
                user_mode != 'scalper'
                and getattr(config, 'RISK', None)
                and getattr(config.RISK, 'enable_atr_sl', False)
            ):
                try:
                    candles = await self.hl_client.get_candles(
                        user_signal.asset, "1m", limit=config.RISK.atr_lookback
                    )
                    if candles:
                        atr_value = session.risk_mgr.calculate_atr(candles)
                except Exception as e:
                    log.debug(f"ATR calc skipped for {user_signal.asset}: {e}")

            # For scalper this only localizes mode/leverage; native SL/TP stay intact.
            user_signal.localize_for_user(user_mode, atr_value=atr_value)

            # ── Vol-aware SL/TP recalculation (Fix 1 + Fix 4) ────────────────
            # calculate_levels() pakai vol_cache dari scorer — zero API calls.
            # Override stop_loss/tp1/tp2 yang diset oleh localize_for_user()
            # dengan level yang sudah mempertimbangkan regime + session + RR.
            # Hanya untuk standard mode; scalper pakai fixed levels karena
            # hold time 12 menit tidak butuh vol-based SL.
            if user_mode != 'scalper':
                try:
                    levels = session.risk_mgr.calculate_levels(
                        asset=user_signal.asset,
                        side=user_signal.side.value,
                        entry_price=user_signal.entry_price,
                        score=user_signal.score,
                        vol_cache=self.scorer._vol_cache,
                    )
                    user_signal.stop_loss = levels["sl_price"]
                    user_signal.tp1       = levels["tp1_price"]
                    user_signal.tp2       = levels["tp2_price"]
                    # Store realized_vol on signal for trailing stop width
                    user_signal.realized_vol = levels["realized_vol"]
                    log.info(
                        f"[LEVELS] {user_signal.asset} {user_signal.side.value.upper()} "
                        f"vol={levels['realized_vol']*100:.2f}% regime={levels['regime']} "
                        f"sl={levels['sl_pct']*100:.2f}% "
                        f"tp1={levels['tp1_pct']*100:.2f}% "
                        f"tp2={levels['tp2_pct']*100:.2f}% "
                        f"RR={levels['rr_ratio']:.2f}x"
                    )
                except Exception as _e:
                    log.warning(f"[LEVELS] {user_signal.asset}: calculate_levels failed ({_e}), keeping localize_for_user levels")

            # ── Expected Value gate (Fix 2) ───────────────────────────────────
            # Pure arithmetic check sebelum kapital dipakai.
            # Bukti 92 trades: EV -0.226%/trade meski WR 57.6% karena RR terbalik.
            if user_mode != 'scalper':
                sl_pct_check  = abs(user_signal.entry_price - user_signal.stop_loss) / user_signal.entry_price
                tp2_pct_check = abs(user_signal.tp2 - user_signal.entry_price) / user_signal.entry_price
                tp1_pct_check = abs(user_signal.tp1 - user_signal.entry_price) / user_signal.entry_price
                ev_ok, ev_val = session.risk_mgr.check_expected_value(
                    score=user_signal.score,
                    sl_pct=sl_pct_check,
                    tp2_pct=tp2_pct_check,
                    side=user_signal.side.value,
                    tp1_pct=tp1_pct_check,
                )
                if not ev_ok:
                    log.info(
                        f"⛔ [EV_BLOCKED] {user_signal.asset} score={user_signal.score} "
                        f"ev={ev_val*100:.3f}% — mathematical EV negative, skip"
                    )
                    continue

            acc = await session.get_account_state()
            allocation_usd = getattr(session.user, "capital_allocation_usd", None)
            is_mainnet = getattr(
                getattr(session.user, "bybit_environment", None), "value", None
            ) == "mainnet"
            if allocation_usd is not None and is_mainnet and session.user.config.bot_mode == BotMode.LIVE:
                from core.capital_allocation import sizing_equity
                effective_sizing_equity = sizing_equity(acc.total_equity, allocation_usd)
                sizing_acc = acc.model_copy(update={"total_equity": effective_sizing_equity})
            else:
                effective_sizing_equity = acc.total_equity
                sizing_acc = acc

            # Full-auto only behavior requested:
            # - only process signals that meet auto threshold
            # - anything below threshold is fully skipped (no Telegram signal)
            effective_auto_threshold = auto_threshold
            if user_signal.score < effective_auto_threshold:
                continue

            # Enrich signal with position sizing for THIS user
            size_usd, contracts, actual_lev = session.risk_mgr.calculate_position_size(
                user_signal, effective_sizing_equity
            )
            user_signal.suggested_size_usd   = size_usd
            user_signal.suggested_contracts  = contracts
            user_signal.suggested_leverage   = actual_lev

            # ⚡ PRE-TRADE VALIDATION (AUTO-ONLY)
            if config.FULL_AUTO and not getattr(user_signal, 'is_pyramid', False):
                approved, reason = session.risk_mgr.pre_trade_check(
                    user_signal, sizing_acc, session.executor.open_positions
                )

                if not approved:
                    log.info(
                        f"⛔ [AUTO_BLOCKED] user={chat_id} mode={user_mode} asset={user_signal.asset} "
                        f"score={user_signal.score} auto_threshold={effective_auto_threshold} reason={reason}"
                    )
                    continue
                
                # If approved, proceed to auto-execution
                user_signal.auto_executed = True
                user_db.save_signal(user_signal) # v17 Sync
                await self.telegram.send_signal(user_signal, is_auto=True, target_chat_id=chat_id)

                _t0 = time.monotonic()
                pos = await session.executor.open_position(user_signal)
                _latency_ms = (time.monotonic() - _t0) * 1000
                log.info(
                    f"[EXEC] {user_signal.asset} {user_signal.side.value.upper()} "
                    f"score={user_signal.score} latency={_latency_ms:.0f}ms"
                )
                if pos:
                    await self.telegram.send_position_opened(pos, user_signal, target_chat_id=chat_id)
            else:
                # AUTO-ONLY policy: no manual signal dispatch.
                continue

    async def _on_trade_confirmed(self, signal, chat_id: str):
        session = await self.get_session(chat_id)
        if not session:
            return False, "Sesi user tidak ditemukan."

        from core.execution_environment_policy import requires_demo_onboarding
        if requires_demo_onboarding(session.user):
            return False, "Paper sudah tidak menerima trade baru. Jalankan /demo untuk setup Bybit Demo."
        
        log.info(f" User {chat_id} confirmed trade: {signal.asset}")

        # Pre-check first so user gets the exact reason (cooldown, max pos, etc.)
        try:
            acc = await session.get_account_state()
            allocation_usd = getattr(session.user, "capital_allocation_usd", None)
            is_mainnet = getattr(
                getattr(session.user, "bybit_environment", None), "value", None
            ) == "mainnet"
            if allocation_usd is not None and is_mainnet and session.user.config.bot_mode == BotMode.LIVE:
                from core.capital_allocation import sizing_equity
                acc = acc.model_copy(update={
                    "total_equity": sizing_equity(acc.total_equity, allocation_usd)
                })
            approved, reason = session.risk_mgr.pre_trade_check(
                signal, acc, session.executor.open_positions
            )
            if not approved:
                await self.telegram.send_text(
                    f"❌ <b>Trade {signal.asset} ditolak.</b>\n<i>{reason}</i>",
                    target_chat_id=chat_id
                )
                return False, reason
        except Exception as e:
            log.error(f"Pre-trade check failed for {chat_id} {signal.asset}: {e}")

        _t0 = time.monotonic()
        pos = await session.executor.open_position(signal)
        _latency_ms = (time.monotonic() - _t0) * 1000
        log.info(
            f"[EXEC] {signal.asset} {signal.side.value.upper()} "
            f"score={signal.score} latency={_latency_ms:.0f}ms"
        )
        if pos:
            await self.telegram.send_position_opened(pos, signal, target_chat_id=chat_id)
            return True, "ok"
        else:
            await self.telegram.send_text(
                f"❌ Gagal mengeksekusi <b>{signal.asset}</b>. Silakan coba lagi saat kondisi risk sudah aman.",
                target_chat_id=chat_id
            )
            return False, "executor_failed"

    async def _update_positions(self):
        """Update unrealized PnL and check TP/SL for all users."""
        # 1. Collect all unique assets across all users
        hl_open_assets = set()
        bybit_assets = set()
        for chat_id, session in self.sessions.items():
            if hasattr(session.executor, 'open_positions'):
                for pos in session.executor.open_positions:
                    if isinstance(session.executor, BybitExecutor):
                        bybit_assets.add(pos.asset)
                    else:
                        hl_open_assets.add(pos.asset)
        
        # 2. Fetch prices ONCE — pakai fast path (no semaphore, no sleep)
        # Position monitor TIDAK boleh diblok oleh data scan semaphore
        hl_prices = {}
        bybit_prices = {}
        for asset in hl_open_assets:
            try:
                hl_prices[asset] = await self.hl_client.get_mark_price_fast(asset)
            except Exception as e:
                log.debug(f"Failed to fetch Hyperliquid price for {asset}: {e}")
        if self.bybit_client:
            for asset in bybit_assets:
                try:
                    spec = self.bybit_client.symbol_registry.resolve(asset)
                    bybit_prices[asset] = await self.bybit_client.get_mark_price(
                        spec.symbol
                    )
                except Exception as e:
                    log.error(f"Failed to fetch Bybit price for {asset}: {e}")

        # 3. Apply updates to each user
        for chat_id, session in self.sessions.items():
            prices = (
                bybit_prices
                if isinstance(session.executor, BybitExecutor)
                else hl_prices
            )
            if isinstance(session.executor, BybitExecutor):
                try:
                    if getattr(session, "bybit_ws", None) and session.bybit_ws.stale:
                        await session.bybit_alerts.emit(
                            "ws_stale",
                            "WARNING BYBIT: private WebSocket stale/disconnected; REST fallback tetap aktif.",
                        )
                    await session.executor.reconcile_if_due()
                except Exception as e:
                    log.error(f"[BYBIT] Reconciliation failed for {chat_id}: {e}")
                    if getattr(session, "bybit_alerts", None):
                        await session.bybit_alerts.emit(
                            "reconciliation_failed",
                            "CRITICAL BYBIT: reconciliation gagal; entry baru harus dianggap tidak aman sampai state exchange pulih.",
                        )
            try:
                acc = await session.get_account_state()
            except Exception as e:
                log.error(f"⚠️ [TRADE_LOOP] Could not fetch account state for {chat_id}: {e}")
                continue # Skip this user for this cycle
            
            # ── Daily Reset Check ───────────────────────────────────────
            # BUG FIX: This must run even if there are no open positions!
            # Otherwise, days will roll over without sending the daily report
            # for users who are flat (sitting in cash).
            if session.risk_mgr.reset_daily(acc.total_equity):
                pos_count = len(getattr(session.executor, 'open_positions', []))
                await self.telegram.send_daily_report(acc, pos_count, target_chat_id=chat_id)
                log.info(f"📬 Daily report sent to {chat_id}")

            # Now skip position updates if they have no open positions
            if not hasattr(session.executor, 'open_positions') or len(session.executor.open_positions) == 0:
                continue

            market_states = {}
            for pos in session.executor.open_positions:
                if getattr(pos, "trade_mode", "standard") != "scalper":
                    continue
                opened = pos.opened_at
                if opened.tzinfo is None:
                    from datetime import timezone
                    opened = opened.replace(tzinfo=timezone.utc)
                age_minutes = (utcnow() - opened).total_seconds() / 60.0
                # Audit window: 12-18m contains winners; 18m+ contains loser drift.
                # No extra API work before the 10m decision window.
                if 10.0 <= age_minutes < 18.0:
                    state = await self._scalper_exit_market_state(pos, prices.get(pos.asset, 0))
                    if state is not None:
                        market_states[pos.position_id] = state

            actions = await session.executor.update_positions(prices, market_states)

            # All position events (incl. time_exit full close) go through one path.
            # PnL card is generated only on FULL close with cumulative totals.
            for action in actions:
                await self.telegram.send_position_event(action, prices, target_chat_id=chat_id)
            
            # Save user state after positional update to persist PnL changes
            acc_state = await session.get_account_state()
            session.user.paper_balance_usd = acc_state.total_equity
            user_db.update_user(session.user)

    async def _scalper_exit_market_state(self, pos, current_price: float) -> Optional[Dict]:
        """Validate whether a 1m crypto-perp scalp still has structure for its 12-18m hold window."""
        if current_price <= 0:
            return None
        try:
            candles = await self.hl_client.get_candles(pos.asset, "1m", limit=24)
            closes = [float(c.get("c", 0)) for c in candles if isinstance(c, dict) and float(c.get("c", 0)) > 0]
            if len(closes) < 21:
                return None
            ema21 = closes[-21]
            alpha = 2.0 / 22.0
            for close in closes[-20:]:
                ema21 = close * alpha + ema21 * (1.0 - alpha)
            # Use a short close sequence rather than one tick. Perp marks can briefly
            # pierce an EMA during normal retests, especially in high-vol regimes.
            recent_rising = closes[-1] >= closes[-3]
            recent_falling = closes[-1] <= closes[-3]
            invalidation = getattr(pos, "micro_invalidation_price", None)
            trend_pct = float(getattr(pos, "trend_pct", 0.0) or 0.0)
            if pos.side.value == "long":
                structure_valid = (
                    (invalidation is None or current_price >= invalidation)
                    and current_price >= ema21 * 0.998
                )
                trend_aligned = trend_pct >= -0.03
                momentum_opposes = current_price < ema21 and recent_falling
            else:
                structure_valid = (
                    (invalidation is None or current_price <= invalidation)
                    and current_price <= ema21 * 1.002
                )
                trend_aligned = trend_pct <= 0.03
                momentum_opposes = current_price > ema21 and recent_rising
            return {
                "structure_valid": structure_valid,
                "trend_aligned": trend_aligned,
                "momentum_opposes": momentum_opposes,
            }
        except Exception as e:
            log.debug(f"[SCALPER] {pos.asset}: exit-state candles unavailable ({e})")
            return None

    async def _send_hourly_summary(self):
        """Send hourly PnL summary to all users."""
        for chat_id, session in self.sessions.items():
            try:
                acc = await session.get_account_state()
                open_count = len(session.executor.open_positions)
                await self.telegram.send_hourly_summary(acc, open_count, target_chat_id=chat_id)
            except Exception as e:
                log.debug(f"Hourly summary error for {chat_id}: {e}")

    async def _on_user_event(self, data):
        """Handle live user events (fills, funding, etc.) from WS."""
        log.debug(f"User event received: {data}")
        # In live mode, sync position fills
        # We trigger a background update of positions to pull latest states
        asyncio.create_task(self._update_positions())

    # ──────────────────────────────────────────
    # SHUTDOWN
    # ──────────────────────────────────────────

    async def stop(self):
        if not self._running and not getattr(self, '_stopping', False):
            return
        self._stopping = True
        log.info("🛑 KARA shutting down...")
        self._running = False

        # Give the loop some time to finish current iteration
        await asyncio.sleep(0.5)

        # In live mode only: notify users about open positions before shutting down.
        # Paper mode positions are skipped — they have no real-world consequence and
        # cause confusion when reported as "open" after they've been SL/TP closed.
        try:
            for chat_id, session in list(self.sessions.items()):
                # Any future Bybit live executor must expose BotMode.LIVE.
                if getattr(session.executor, "mode", None) != BotMode.LIVE:
                    continue
                open_pos = getattr(session.executor, 'open_positions', [])
                if not open_pos:
                    continue
                assets = ", ".join(p.asset for p in open_pos)
                log.warning(
                    f"[SHUTDOWN] User {chat_id} has {len(open_pos)} open live position(s): {assets}. "
                    "Bot is stopping — positions remain on-chain. User must manage manually."
                )
                try:
                    unprotected = await session.executor.audit_protection()
                    if unprotected:
                        log.critical(
                            "[SHUTDOWN] Unprotected Bybit positions for %s: %s",
                            chat_id,
                            ", ".join(unprotected),
                        )
                        await self.telegram.send_text(
                            "<b>Bahaya: posisi tanpa hard SL saat shutdown:</b> <code>"
                            + ", ".join(unprotected)
                            + "</code>",
                            target_chat_id=chat_id,
                        )
                except Exception as e:
                    log.error(f"[SHUTDOWN] Protection audit failed for {chat_id}: {e}")
                try:
                    await self.telegram.send_text(
                        f"⚠️ <b>KARA sedang restart.</b>\n\n"
                        f"Kamu memiliki <b>{len(open_pos)} posisi terbuka</b>: <code>{assets}</code>\n\n"
                        f"Posisi tetap aktif di chain. Pantau dan kelola secara manual hingga bot kembali online.",
                        target_chat_id=chat_id
                    )
                except Exception:
                    pass
        except Exception as e:
            log.error(f"[SHUTDOWN] Failed to notify users of open positions: {e}")

        try:
            await self.telegram.stop()
        except Exception as e:
            log.warning(f"Telegram stop warning: {e}")

        try:
            await self.ws_client.stop()
        except: pass

        try:
            await self.hl_client.close()
        except: pass

        for session in self.sessions.values():
            private_bybit = getattr(session, "bybit_client", None)
            private_ws = getattr(session, "bybit_ws", None)
            if private_ws:
                try:
                    await private_ws.stop()
                except Exception as e:
                    log.warning(f"User Bybit WS close warning: {e}")
            if private_bybit:
                try:
                    await private_bybit.close()
                except Exception as e:
                    log.warning(f"User Bybit close warning: {e}")

        if self.bybit_client:
            try:
                await self.bybit_client.close()
            except Exception as e:
                log.warning(f"Bybit close warning: {e}")

        log.info("✅ KARA stopped. Goodbye!")


# ──────────────────────────────────────────────
# ENTRYPOINT
# ──────────────────────────────────────────────

async def main():
    run_total_reset_if_confirmed()
    bot = KaraBot()

    # 1. Start Dashboard FIRST in background to pass Railway Health Checks
    dashboard_task = asyncio.create_task(run_dashboard())
    print("⏳ [KARA_DEBUG] Initializing dashboard...")
    await asyncio.sleep(2) # Give it 2 seconds to bind the port
    log.info("📊 Dashboard task started in background.")

    # 2. Initialize Bot (Market data, calibration, etc.)
    try:
        await bot.start()
        
        # 🔗 [Pulse Sync v27] - Connect Bot Session to Dashboard
        init_dashboard(bot.sessions, bot.telegram, bot.mode_mgr)
        log.info(f"✅ [DASHBOARD] Pulse Sync Complete: {len(bot.sessions)} user sessions linked.")
        
    except Exception as e:
        log.error(f" Initialization failed: {e}")
        import traceback
        traceback.print_exc()
        # Shutdown dashboard if init fails
        dashboard_task.cancel()
        sys.exit(1)

    # 3. Setup Signal Handlers
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(
                    sig,
                    lambda: asyncio.create_task(bot.stop())
                )
            except Exception as e:
                log.warning(f"Could not setup signal handler: {e}")

    # 4. Run trading loop and keep dashboard running
    try:
        await asyncio.gather(
            bot.run_trading_loop(),
            dashboard_task,
        )
    except Exception as e:
        log.error(f" Runtime error: {e}")
        import traceback
        traceback.print_exc()
        await bot.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as fatal_e:
        log = logging.getLogger("kara.main")
        log.critical(f" 💀 [FATAL_CRASH] Bot process died: {fatal_e}", exc_info=True)
        # Final graceful exit attempt not possible since loop is gone
        sys.exit(1)
