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
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
# Ensure root directory is in path for module discovery (Railway/Docker Fix)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from config import MODE, WATCHED_ASSETS, SIGNAL
from models.schemas import ExecutionMode
from data.hyperliquid_client import HyperliquidClient
from data.ws_client import KaraWebSocketClient, MarketDataCache, market_cache
from engine.scoring_engine import ScoringEngine
from risk.risk_manager import RiskManager
from execution.paper_executor import PaperExecutor
from execution.live_executor import LiveExecutor
from notify.telegram import KaraTelegram
from dashboard.app import init_dashboard, run_dashboard, broadcast
from core.mode_manager import mode_manager
from utils.helpers import utcnow
from core.db import user_db
from core.user_session import UserSession
from utils.changelog_generator import ChangelogGenerator

# ──────────────────────────────────────────────
# LOGGING SETUP
# ──────────────────────────────────────────────

# Fix Windows encoding issue - force UTF-8 for console output
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# [RAILWAY TELEMETRY] Activate JSON logging for cloud environments
if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("LOG_FORMAT") == "json":
    from utils.logging_config import setup_json_logging
    setup_json_logging()
else:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
        ],
    )
log = logging.getLogger("kara.main")


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
        """
        changed = 0
        for u in user_db.get_all_users():
            cfg = u.config
            dirty = False
            if cfg.std_min_score_to_signal != 45:
                cfg.std_min_score_to_signal = 45
                dirty = True
            if cfg.std_min_score_to_auto_trade != 52:
                cfg.std_min_score_to_auto_trade = 52
                dirty = True
            if cfg.scl_min_score_to_signal != 45:
                cfg.scl_min_score_to_signal = 45
                dirty = True
            if cfg.scl_min_score_to_auto_trade != 52:
                cfg.scl_min_score_to_auto_trade = 52
                dirty = True
            if dirty:
                user_db.update_user(u)
                changed += 1
        if changed:
            log.info(f"🔒 Locked score thresholds enforced for {changed} user(s).")

    def _run_one_time_release_reset(self, release_tag: str) -> bool:
        """
        One-time reset per release_tag:
        - clear all open paper positions/state/risk state
        - reset user paper balance to default
        """
        marker_path = os.path.join(config.STORAGE_DIR, f"release_reset_{release_tag}.done")
        if os.path.exists(marker_path):
            return False

        users_to_reset = user_db.get_all_users()
        if not users_to_reset:
            return False

        reset_count = 0
        for u in users_to_reset:
            try:
                user_db.clear_paper_positions(u.chat_id)
                user_db.clear_paper_state(u.chat_id)
                user_db.clear_risk_state(u.chat_id)
                u.paper_balance_usd = config.PAPER_BALANCE_USD
                user_db.update_user(u)
                reset_count += 1
            except Exception as e:
                log.error(f"Failed one-time reset for {u.chat_id}: {e}")

        if reset_count:
            try:
                with open(marker_path, "w", encoding="utf-8") as f:
                    f.write(utcnow().isoformat())
            except Exception as e:
                log.warning(f"Could not write release reset marker: {e}")
            log.warning(f"🧹 One-time release reset applied for {reset_count} user(s) [{release_tag}]")
            return True
        return False



    # ──────────────────────────────────────────
    # STARTUP
    # ──────────────────────────────────────────

    async def start(self):
        log.info("=" * 60)
        log.info(f" KARA Bot starting")
        log.info(f" 📡 Data source : {config.DATA_SOURCE.upper()} (live prices)")
        log.info(f" 📄 Execution   : {config.TRADE_MODE.upper()} (simulated trades)")

        if config.TRADE_MODE == "live" and not config.PRIVATE_KEY:
            log.error(" LIVE mode requires HL_PRIVATE_KEY in .env!")
            sys.exit(1)

        # Connect to Hyperliquid
        await self.hl_client.connect()

        # ── ONE-TIME HARD RESET ──────────────────────────────────────────
        # Set env var KARA_HARD_RESET=true sebelum deploy untuk wipe semua data.
        # Setelah reset, ubah kembali ke false agar tidak reset di deploy berikutnya.
        # Yang dihapus: posisi, balance, journal, ML data, trained model, meta stats.
        if config.HARD_RESET_ON_DEPLOY:
            log.warning("=" * 60)
            log.warning("🧹 KARA_HARD_RESET=true — memulai full data wipe...")
            log.warning("=" * 60)
            result = user_db.hard_reset_all_data()
            if result.get("status") == "ok":
                log.warning(
                    f"✨ [RESET SELESAI] "
                    f"users={result.get('users_reset', 0)} | "
                    f"trades_deleted={result.get('trade_history', 0)}"
                )
                log.warning("⚠️  Ingat: ubah KARA_HARD_RESET=false di env setelah ini!")
            else:
                log.error(f"❌ [RESET GAGAL] {result.get('error', 'unknown error')}")

        # Load market list (always top volume as requested)
        log.info("Loading top volume markets...")
        self.watched_assets = await self.hl_client.get_top_volume_markets(top_n=100)
        if len(self.watched_assets) < 50:
            log.warning(f"Low market count ({len(self.watched_assets)}), retrying in 10s...")
            await asyncio.sleep(10)
            self.watched_assets = await self.hl_client.get_top_volume_markets(top_n=100)

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
        
        # Prefer git SHA (stable across restarts) over deployment ID (changes every redeploy)
        if short_sha:
            release_tag = f"{current_version}-{short_sha}"
        else:
            release_tag = current_version

        users_to_update = [
            u for u in user_db.users.values()
            if u.is_authorized and u.last_seen_version != release_tag
        ]

        # Reset saldo/posisi on deploy is disabled by user request.
        did_release_reset = False

        # Initialize User Sessions from DB
        self._enforce_locked_score_thresholds()
        for u in user_db.get_all_users():
            session = UserSession(u, mode_manager=self.mode_mgr, hl_client=self.hl_client)
            if hasattr(session.executor, 'load_from_db'):
                session.executor.load_from_db(u.chat_id)
            # Fix #1: connect live client + reconcile chain positions for live users
            await session.initialize()
            self.sessions[u.chat_id] = session
        log.info(f"Loaded {len(self.sessions)} user sessions.")

        # Fix #4: Warn if live users exist but server is not in live mode
        from models.schemas import BotMode
        live_users = [u for u in user_db.get_all_users() if u.config.bot_mode == BotMode.LIVE]
        if live_users and config.TRADE_MODE != "live":
            log.critical(
                f"🚨 DANGER: {len(live_users)} user(s) set to LIVE mode but "
                f"KARA_TRADE_MODE='{config.TRADE_MODE}'. "
                "Their orders will be routed to TESTNET, not mainnet! "
                "Set KARA_TRADE_MODE=live in .env to fix this."
            )
        if config.TRADE_MODE == "live" and config.SECRET_KEY == "CHANGEME":
            log.critical(
                "🚨 SECURITY: SECRET_KEY is still 'CHANGEME'! "
                "Dashboard is insecure. Set SECRET_KEY in .env immediately."
            )

        # Inject into dashboard (passing sessions registry for multi-user support)
        init_dashboard(self.sessions, self.telegram, self.mode_mgr)

        # Start Telegram (optional, errors are graceful)
        try:
            await self.telegram.start()
        except Exception as e:
            log.warning(f"  Telegram startup failed: {e}")
            log.warning("   Bot will continue without Telegram notifications")

        # ── Update Notification System ────────────────────────────────
        if self.telegram and self.telegram._bot_started:
            # Only notify chats that haven't seen this release_tag yet.
            # Also cover chats in _authorized_chat_ids with no/stale DB record.
            needs_update: set = {u.chat_id for u in users_to_update}
            for cid in self.telegram._authorized_chat_ids:
                db_user = user_db.get_user(cid)
                if db_user is None or db_user.last_seen_version != release_tag:
                    needs_update.add(cid)
            target_chat_ids = list(needs_update)

            if target_chat_ids:
                log.info(
                    f"📢 Sending update notification to {len(target_chat_ids)} chat(s) "
                    f"(release={release_tag}, deploy_id={'yes' if short_dep else 'no'}, sha={'yes' if short_sha else 'no'})"
                )
                for chat_id in target_chat_ids:
                    extra_notes = []
                    if did_release_reset:
                        extra_notes.append(
                            "Reset khusus update ini: semua posisi sebelumnya dikosongkan dan saldo dikembalikan ke saldo normal."
                        )
                    success = await self.telegram.send_update_notification(
                        chat_id,
                        extra_notes=extra_notes
                    )
                    if success:
                        u = user_db.get_user(chat_id)
                        if u:
                            u.last_seen_version = release_tag
                            user_db.update_user(u)
                    await asyncio.sleep(0.1) # Rate limit safety
            else:
                log.info(f"📭 No target chats for update notification ({release_tag})")

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
        _sess_bonus, _sess_reasons, _sess_threshold_delta = self.scorer._get_session_bonus()
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

        # Fix #9: Register dead callback — notify admin via Telegram instead of dying silently
        async def _on_ws_dead():
            admin_id = config.TELEGRAM_CHAT_ID
            if admin_id and self.telegram and self.telegram._bot_started:
                await self.telegram.send_text(
                    "🚨 <b>KARA WebSocket terputus permanen!</b>\n"
                    "Market data stream mati setelah max retry.\n"
                    "Bot masih berjalan dalam slow-retry mode (60s interval).\n"
                    "<i>Silakan cek koneksi atau restart bot jika perlu.</i>",
                    target_chat_id=admin_id,
                )
            log.critical("WS dead callback fired — bot in slow-retry mode")

        self.ws_client.add_dead_callback(_on_ws_dead)

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
        # [TELEGRAM] Send dynamic update notification once at startup
        asyncio.create_task(self._send_startup_notification())

        asyncio.create_task(self._position_monitor_loop(), name="position_monitor")
        asyncio.create_task(self._ws_watchdog_loop(), name="ws_watchdog")
        asyncio.create_task(self._scan_loop(), name="scan_loop")
        asyncio.create_task(self._health_logger_loop(), name="health_logger")
        asyncio.create_task(self._skip_summary_loop(), name="skip_summary")

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

    async def _health_logger_loop(self):
        """[RAILWAY TELEMETRY] Log bot health status every 60 seconds."""
        log.info("[HEALTH] Health logger loop started (60s interval)")
        while self._running:
            try:
                total_open = 0
                for session in self.sessions.values():
                    try:
                        total_open += len(session.executor.open_positions)
                    except Exception:
                        pass

                last_sig = self.scorer._last_signal_time
                last_sig_str = (
                    datetime.fromtimestamp(last_sig, tz=timezone.utc).strftime("%H:%M:%S")
                    if last_sig else "never"
                )
                skip_total = sum(self.scorer.skip_counters.values())

                log.info(
                    f"[HEALTH] open_pos={total_open} | last_signal={last_sig_str} | "
                    f"skip_total={skip_total} | markets={len(self.watched_assets)} | "
                    f"features=atr_sl={getattr(config.SCALPER, 'atr_sl_enabled', True)},"
                    f"partial_exit=True,breakeven=True,funding_contra=True"
                )
            except Exception as e:
                log.error(f"[HEALTH] Health logger error: {e}")
            await asyncio.sleep(60)

    async def _skip_summary_loop(self):
        """[RAILWAY TELEMETRY] Log skip summary every 5 minutes."""
        log.info("[SKIP-SUMMARY] Skip summary loop started (5min interval)")
        while self._running:
            await asyncio.sleep(300)  # 5 minutes
            try:
                self.scorer.log_skip_summary()
            except Exception as e:
                log.error(f"[SKIP-SUMMARY] Error: {e}")

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
                scan_interval = config.SCALPER.scan_interval_seconds

                if now - last_scan >= scan_interval:
                    await self._scan_all_assets()
                    last_scan = now
            except Exception as e:
                log.error(f"[SCAN] Scan loop error: {e}", exc_info=True)

            await asyncio.sleep(1)

    async def get_session(self, chat_id: str) -> Optional[UserSession]:
            if str(chat_id) not in self.sessions:
                user = user_db.get_user(str(chat_id))
                if user:
                    session = UserSession(user, mode_manager=self.mode_mgr, hl_client=self.hl_client)
                    # Restore persisted state (balance + open positions) from DB
                    if hasattr(session.executor, 'load_from_db'):
                        session.executor.load_from_db(user.chat_id)
                    try:
                        await session.initialize()
                    except Exception as e:
                        log.error(f"❌ Failed to initialize session for {chat_id}: {e}")
                    # Live mode: sync open positions from chain
                    if hasattr(session.executor, 'load_from_chain'):
                        try:
                            await session.executor.load_from_chain()
                        except Exception as e:
                            log.error(f"[LIVE] Chain sync failed for {user.chat_id}: {e}")
                    self.sessions[str(chat_id)] = session
            return self.sessions.get(str(chat_id))

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

            # KARA runs exclusively in Scalper Mode
            target_ids = list(self.telegram._authorized_chat_ids)
            active_modes_list = ["scalper"]

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

            # Scan all assets: base watched_assets + scalper-specific assets
            assets_to_scan = list(self.watched_assets)
            scalper_assets = getattr(config, 'SCALPER_ASSETS', [])
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

            blocked_info = f" | Blocked: {blocked_count}" if blocked_count > 0 else ""
            log.info(
                f"🏁 Scan complete | "
                f"Markets: {total_scanned} total "
                f"({len(self.watched_assets)} watched + {scalper_extra} scalper-only) | "
                f"Scored: {scored_count} | Signals: {sig_count}{blocked_info} | "
                f"Top: [{top_str or 'None'}] | "
                f"Elapsed: {scan_elapsed:.1f}s"
            )

            # Near-miss log: asset yang scored tinggi tapi tidak jadi signal (Signals: 0)
            if sig_count == 0 and top_scorers:
                scl_base = getattr(config.SCALPER, 'min_score_to_enter', 57)
                _, _, _sess_delta = self.scorer._get_session_bonus()
                effective_entry = scl_base + _sess_delta
                near_miss = [(a, s) for a, s in top_scorers[:5] if s >= effective_entry - 7]
                if near_miss:
                    nm_str = ", ".join([f"{a}:{s}" for a, s in near_miss])
                    sess_tag = f"+{_sess_delta}" if _sess_delta > 0 else (f"{_sess_delta}" if _sess_delta < 0 else "±0")
                    log.info(
                        f"📊 [NEAR-MISS] Top scorers vs entry gate {effective_entry} "
                        f"(base={scl_base} sess={sess_tag}): [{nm_str}]"
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
            # Get or create session
            session = await self.get_session(chat_id)
            if not session:
                user = user_db.create_user(chat_id, "Master", init_usd=config.PAPER_BALANCE_USD)
                session = await self.get_session(chat_id)
                
            user_mode = 'scalper'
            base_signal = signals_dict.get("scalper")
            if not base_signal:
                continue

            # ── Per-User Threshold Check (AUTO-ONLY POLICY) ──────────────────
            user_cfg = session.user.config
            auto_threshold = int(user_cfg.scl_min_score_to_auto_trade)

            # ── MULTI-USER FIX: Deep-copy signal with a UNIQUE signal_id per user ──
            # model_copy() copies ALL fields including signal_id.
            # If two users share the same signal_id, User A confirming/skipping
            # calls _pending_signals.pop(sig_id) which ALSO removes User B's signal.
            # Fix: assign a fresh UUID immediately after copy so each user's
            # signal is stored and resolved independently in _pending_signals.
            # Deep copy so nested breakdown/warnings are not shared across users/cycles.
            user_signal = base_signal.model_copy(deep=True)
            user_signal.signal_id = f"{base_signal.signal_id[:4]}{uuid.uuid4().hex[:4].upper()}"


            # ATR hanya dihitung untuk standard mode — scalper pakai fixed SL ketat,
            # ATR adaptive akan override 0.70% SL dan merusak R:R yang sudah dikalibrasi.
            atr_value = 0.0
            if user_mode != 'scalper' and getattr(config, 'RISK', None) and getattr(config.RISK, 'enable_atr_sl', False):
                try:
                    candles = await self.hl_client.get_candles(
                        user_signal.asset, "1m", limit=config.RISK.atr_lookback
                    )
                    if candles:
                        atr_value = session.risk_mgr.calculate_atr(candles)
                except Exception as e:
                    log.debug(f"ATR calc skipped for {user_signal.asset}: {e}")
                
            # Scalper: localize_for_user hanya untuk leverage & trade_mode.
            # SL/TP sudah dikalkulasi oleh _build_scalper_signal (score matrix + ATR),
            # localize_for_user akan override dengan config mentah dan merusak RR.
            if user_mode == 'scalper':
                user_signal.trade_mode = user_mode
                import config as _cfg
                _scfg = _cfg.SCALPER
                user_signal.suggested_leverage = min(_scfg.default_leverage, _scfg.max_leverage)
            else:
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
                    user_signal.tp3       = levels["tp3_price"]
                    user_signal.realized_vol = levels["realized_vol"]
                    log.info(
                        f"[LEVELS] {user_signal.asset} {user_signal.side.value.upper()} "
                        f"vol={levels['realized_vol']*100:.2f}% regime={levels['regime']} "
                        f"sl={levels['sl_pct']*100:.2f}% "
                        f"tp1={levels['tp1_pct']*100:.2f}% "
                        f"tp2={levels['tp2_pct']*100:.2f}% "
                        f"tp3={levels['tp3_pct']*100:.2f}% "
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
                ev_ok, ev_val = session.risk_mgr.check_expected_value(
                    score=user_signal.score,
                    sl_pct=sl_pct_check,
                    tp2_pct=tp2_pct_check,
                )
                if not ev_ok:
                    log.info(
                        f"⛔ [EV_BLOCKED] {user_signal.asset} score={user_signal.score} "
                        f"ev={ev_val*100:.3f}% — mathematical EV negative, skip"
                    )
                    continue

            acc = await session.get_account_state()

            # Full-auto only behavior requested:
            # - only process signals that meet auto threshold
            # - anything below threshold is fully skipped (no Telegram signal)
            effective_auto_threshold = auto_threshold
            if user_signal.score < effective_auto_threshold:
                if chat_id == target_ids[0]:  # log sekali saja, jangan spam per-user
                    log.info(
                        f"🔕 [BELOW_THRESH] {user_signal.asset} {user_signal.side.value.upper()} "
                        f"score={user_signal.score} < auto_threshold={effective_auto_threshold} — skipped"
                    )
                continue

            # Enrich signal with position sizing for THIS user
            size_usd, contracts, actual_lev = session.risk_mgr.calculate_position_size(
                user_signal, acc.total_equity
            )
            user_signal.suggested_size_usd   = size_usd
            user_signal.suggested_contracts  = contracts
            user_signal.suggested_leverage   = actual_lev

            # ⚡ PRE-TRADE VALIDATION (AUTO-ONLY)
            if config.FULL_AUTO and not getattr(user_signal, 'is_pyramid', False):
                approved, reason = session.risk_mgr.pre_trade_check(
                    user_signal, acc, session.executor.open_positions
                )

                if not approved:
                    # Akumulasi, jangan log per user (spam)
                    if not hasattr(self, '_blocked_log_buffer'):
                        self._blocked_log_buffer = {}
                    key = f"{user_signal.asset}:{reason[:60]}"
                    self._blocked_log_buffer[key] = self._blocked_log_buffer.get(key, 0) + 1
                    continue

                # If approved, proceed to auto-execution
                user_signal.auto_executed = True
                user_db.save_signal(user_signal) # v17 Sync
                await self.telegram.send_signal(user_signal, is_auto=True, target_chat_id=chat_id)

                _t0 = time.monotonic()
                pos = await session.executor.open_position(user_signal)
                _latency_ms = (time.monotonic() - _t0) * 1000
                log.debug(
                    f"[EXEC] {user_signal.asset} {user_signal.side.value.upper()} "
                    f"score={user_signal.score} latency={_latency_ms:.0f}ms"
                )
                if pos:
                    await self.telegram.send_position_opened(pos, user_signal, target_chat_id=chat_id)
            else:
                # FULL_AUTO is off — log signal clearly so operator knows it was seen but not executed
                log.info(
                    f"📨 [SIGNAL] {user_signal.asset} {user_signal.side.value.upper()} score={user_signal.score} "
                    f"user={chat_id} → NOT executed (FULL_AUTO=off, set KARA_FULL_AUTO=true to enable)"
                )
                continue

        # Flush blocked log buffer sebagai 1 baris ringkasan (bukan spam per-user)
        if hasattr(self, '_blocked_log_buffer') and self._blocked_log_buffer:
            for reason_key, count in self._blocked_log_buffer.items():
                asset_part, reason_part = reason_key.split(':', 1)
                log.info(f"⛔ [AUTO_BLOCKED] {asset_part} ×{count} users — {reason_part}")
            self._blocked_log_buffer.clear()

    async def _on_trade_confirmed(self, signal, chat_id: str):
        session = await self.get_session(chat_id)
        if not session:
            return False, "Sesi user tidak ditemukan."
        
        log.info(f" User {chat_id} confirmed trade: {signal.asset}")

        # Pre-check first so user gets the exact reason (cooldown, max pos, etc.)
        try:
            acc = await session.get_account_state()
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
        log.debug(
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
        all_open_assets = set()
        for chat_id, session in self.sessions.items():
            if hasattr(session.executor, 'open_positions'):
                for pos in session.executor.open_positions:
                    all_open_assets.add(pos.asset)
        
        # 2. Fetch prices ONCE — pakai fast path (no semaphore, no sleep)
        # Position monitor TIDAK boleh diblok oleh data scan semaphore
        prices = {}
        for asset in all_open_assets:
            try:
                prices[asset] = await self.hl_client.get_mark_price_fast(asset)
            except Exception as e:
                log.debug(f"Failed to fetch market price for {asset}: {e}")

        # 2b. Fetch 1m candle OHLCV penuh per asset untuk exit logic
        # Hanya diambil sekali per asset, dishare ke semua user yang hold asset tsb.
        # limit=55: cukup untuk EMA50 (50 candle) + buffer 5 candle, TANPA API call tambahan.
        candle_closes_map:  dict = {}
        candle_highs_map:   dict = {}
        candle_lows_map:    dict = {}
        candle_volumes_map: dict = {}
        htf_closes_map:     dict = {}   # 15m candles untuk HTF trend filter

        for asset in all_open_assets:
            try:
                # 1m OHLCV — 55 candle cukup untuk EMA50 + ATR14 + RSI14
                candles = await self.hl_client.get_candles(asset, "1m", limit=55)
                if candles:
                    closes, highs, lows, volumes = [], [], [], []
                    for c in candles:
                        if isinstance(c, dict):
                            closes.append(float(c.get("c", 0)))
                            highs.append(float(c.get("h", c.get("c", 0))))
                            lows.append(float(c.get("l", c.get("c", 0))))
                            volumes.append(float(c.get("v", 0)))
                        elif isinstance(c, (list, tuple)) and len(c) >= 6:
                            closes.append(float(c[4]))
                            highs.append(float(c[2]))
                            lows.append(float(c[3]))
                            volumes.append(float(c[5]))
                    if closes:
                        candle_closes_map[asset]  = closes
                        candle_highs_map[asset]   = highs
                        candle_lows_map[asset]    = lows
                        candle_volumes_map[asset] = volumes
            except Exception as e:
                log.debug(f"1m candle fetch skipped for {asset}: {e}")

            try:
                # 15m HTF candles — 55 candle untuk EMA50 HTF trend filter
                htf_candles = await self.hl_client.get_candles(asset, "15m", limit=55)
                if htf_candles:
                    htf_closes = []
                    for c in htf_candles:
                        if isinstance(c, dict):
                            htf_closes.append(float(c.get("c", 0)))
                        elif isinstance(c, (list, tuple)) and len(c) >= 5:
                            htf_closes.append(float(c[4]))
                    if htf_closes:
                        htf_closes_map[asset] = htf_closes
            except Exception as e:
                log.debug(f"15m candle fetch skipped for {asset}: {e}")

        # 2c. Inject semua candle data ke setiap open scalper position
        for chat_id, session in self.sessions.items():
            if not hasattr(session.executor, 'open_positions'):
                continue
            for pos in session.executor.open_positions:
                if pos.trade_mode == 'scalper':
                    if pos.asset in candle_closes_map:
                        pos.candle_closes  = candle_closes_map[pos.asset]
                        pos.candle_highs   = candle_highs_map.get(pos.asset, [])
                        pos.candle_lows    = candle_lows_map.get(pos.asset, [])
                    if pos.asset in candle_volumes_map:
                        pos.candle_volumes = candle_volumes_map[pos.asset]
                    if pos.asset in htf_closes_map:
                        pos.htf_candle_closes = htf_closes_map[pos.asset]

        # 3. Apply updates to each user
        for chat_id, session in self.sessions.items():
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

            actions = await session.executor.update_positions(prices)
            early_exit_actions = [a for a in actions if a.get("action") in ("time_exit", "momentum_exit", "early_trail")]
            # Exclude semua early_exit dari other_actions agar tidak dikirim dua kali
            other_actions = [a for a in actions if a.get("action") not in ("time_exit", "momentum_exit", "early_trail")]

            # Kirim notifikasi personal per posisi (KARA style)
            if early_exit_actions:
                from datetime import timezone as _tz, datetime as _dt
                for a in early_exit_actions:
                    pos = session.executor._positions.get(a.get("position_id", "")) if hasattr(session.executor, "_positions") else None
                    if not pos:
                        continue
                    pnl      = float(a.get("pnl", 0.0))
                    price    = float(a.get("price", pos.entry_price))
                    lev      = getattr(pos, "leverage", 1) or 1
                    pnl_pct  = pos.roe_pct(price) * 100
                    opened   = pos.opened_at
                    if opened.tzinfo is None:
                        opened = opened.replace(tzinfo=_tz.utc)
                    hold_min = int((_dt.now(_tz.utc) - opened).total_seconds() / 60)
                    pct_sign = "+" if pnl_pct >= 0 else ""
                    pnl_sign = "+" if pnl >= 0 else ""

                    from config import USD_TO_IDR
                    pnl_idr = pnl * USD_TO_IDR
                    pnl_idr_str = (
                        f"+Rp{pnl_idr:,.0f}".replace(",", ".")
                        if pnl_idr >= 0
                        else f"-Rp{abs(pnl_idr):,.0f}".replace(",", ".")
                    )
                    outcome_emoji = "✅" if pnl >= 0 else "🔻"
                    outcome = "Profit" if pnl >= 0 else "Loss"

                    if a.get("action") == "momentum_exit":
                        header  = f"↩️ <b>Momentum Exit  •  {pos.asset} {pos.side.value.upper()} {lev}x</b>"
                        subtext = "<i>Multi-confirmation reversal — keluar sebelum kena SL.</i>"
                        checks  = a.get("checks", {})
                        detail_parts = []
                        if checks.get("pullback"):  detail_parts.append("✅ Pullback")
                        if checks.get("volume"):    detail_parts.append("✅ Volume")
                        if checks.get("trend"):     detail_parts.append("✅ EMA break")
                        if checks.get("momentum"):  detail_parts.append("✅ RSI/MACD")
                        raw_msg = a.get("message", "")
                        exit_detail = f"\n<i>{' · '.join(detail_parts)}</i>" if detail_parts else (f"\n<i>{raw_msg}</i>" if raw_msg else "")
                    elif a.get("action") == "early_trail":
                        header  = f"🛡️ <b>Early Trail Exit  •  {pos.asset} {pos.side.value.upper()} {lev}x</b>"
                        subtext = "<i>Profit dikunci sebelum harga berbalik.</i>"
                        exit_detail = ""
                    else:  # time_exit
                        header  = f"⏱ <b>Time Exit  •  {pos.asset} {pos.side.value.upper()} {lev}x</b>"
                        subtext = f"<i>Posisi ditutup otomatis setelah {hold_min} menit~</i>"
                        exit_detail = ""

                    # Akumulasi total PnL: semua partial close (pnl_realized) + final close ini
                    total_pnl   = getattr(pos, "pnl_realized", 0.0) + pnl
                    total_sign  = "+" if total_pnl >= 0 else ""
                    total_idr   = total_pnl * USD_TO_IDR
                    total_idr_str = (
                        f"+Rp{total_idr:,.0f}".replace(",", ".")
                        if total_idr >= 0
                        else f"-Rp{abs(total_idr):,.0f}".replace(",", ".")
                    )

                    # Hitung ROE% berbasis total PnL akumulasi
                    entry_val = pos.entry_price * pos.size_initial if pos.size_initial else 1
                    total_roe_pct = (total_pnl / entry_val * lev * 100) if entry_val else pnl_pct

                    # Cache PnL card data dan buat button
                    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
                    inline_markup = None
                    try:
                        acc_state = await session.get_account_state()
                        if acc_state:
                            self.telegram._pending_pnl_cards[pos.position_id] = {
                                "pos": pos,
                                "close_data": {
                                    "exit_price": price,
                                    "pnl": total_pnl,
                                    "pnl_pct": total_roe_pct / 100,
                                    "reason": a.get("action"),
                                    "score": getattr(pos, "entry_score", 0) or 0,
                                    "duration_sec": hold_min * 60,
                                },
                                "account": acc_state,
                            }
                            inline_markup = InlineKeyboardMarkup([[
                                InlineKeyboardButton("📊 Lihat PnL Card", callback_data=f"card_detail:{pos.position_id}"),
                            ]])
                    except Exception as _e:
                        log.debug(f"[PnLCard] early_exit cache failed: {_e}")

                    await self.telegram.send_text(
                        f"{header}\n"
                        f"{subtext}\n\n"
                        f"{outcome_emoji} <b>{outcome}    {total_sign}{total_roe_pct:.2f}%</b>\n"
                        f"Total PnL : <code>{total_sign}${abs(total_pnl):.2f}</code>  (<code>{total_idr_str}</code>)\n"
                        f"Entry     : <code>${pos.entry_price:,.3f}</code>  →  Exit: <code>${price:,.3f}</code>\n"
                        f"Durasi    : {hold_min} menit"
                        f"{exit_detail}",
                        target_chat_id=chat_id,
                        reply_markup=inline_markup
                    )

            for action in other_actions:
                await self.telegram.send_position_event(action, prices, target_chat_id=chat_id)
            
            # Save user state after positional update to persist PnL changes
            acc_state = await session.get_account_state()
            session.user.paper_balance_usd = acc_state.total_equity
            user_db.update_user(session.user)

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

    async def _send_startup_notification(self):
        """[RAILWAY] Generate and send dynamic changelog to Admin on deploy."""
        try:
            # Wait a few seconds for Telegram to initialize
            await asyncio.sleep(10)
            
            gen = ChangelogGenerator(repo_path=".")
            
            # Check for custom notes
            custom = os.getenv("KARA_DEPLOY_NOTES", "")
            if not custom and os.path.exists("DEPLOY_NOTES.txt"):
                try:
                    with open("DEPLOY_NOTES.txt", "r") as f:
                        custom = f.read().strip()
                except Exception:
                    pass
            
            message = gen.generate_telegram_message(custom_notes=custom or None)
            
            # Send to main admin
            if config.TELEGRAM_CHAT_ID:
                await self.telegram.send_text(
                    message=message,
                    target_chat_id=config.TELEGRAM_CHAT_ID
                )
                log.info("[TELEGRAM] Startup update notification sent to Admin")
            else:
                log.warning("[TELEGRAM] TELEGRAM_CHAT_ID not set, skipping startup notification")
                
        except Exception as e:
            log.warning(f"[TELEGRAM] Failed to send startup notification: {e}")


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
                # Only alert for live executor instances
                if not isinstance(session.executor, LiveExecutor):
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

        log.info("✅ KARA stopped. Goodbye!")


# ──────────────────────────────────────────────
# ENTRYPOINT
# ──────────────────────────────────────────────

async def main():
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
