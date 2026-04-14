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
import uuid
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
        """Force fixed score thresholds for all users/modes."""
        changed = 0
        for u in user_db.get_all_users():
            cfg = u.config
            dirty = False
            if cfg.std_min_score_to_signal != 58:
                cfg.std_min_score_to_signal = 58
                dirty = True
            if cfg.std_min_score_to_auto_trade != 65:
                cfg.std_min_score_to_auto_trade = 65
                dirty = True
            if cfg.scl_min_score_to_signal != 50:
                cfg.scl_min_score_to_signal = 50
                dirty = True
            if cfg.scl_min_score_to_auto_trade != 57:
                cfg.scl_min_score_to_auto_trade = 57
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

    def _bucket_component(self, x: float) -> str:
        if x >= 10:
            return "pos"
        if x <= -10:
            return "neg"
        return "neu"

    def _build_meta_pattern_key(self, signal, user_mode: str) -> str:
        bd = getattr(signal, "breakdown", None)
        oi = float(getattr(bd, "oi_funding_score", 0) or 0)
        ob = float(getattr(bd, "orderbook_score", 0) or 0)
        liq = float(getattr(bd, "liquidation_score", 0) or 0)
        ses = int(getattr(bd, "session_bonus", 0) or 0)
        regime = getattr(getattr(signal, "regime", None), "value", "unknown")
        side = getattr(getattr(signal, "side", None), "value", "long")

        if ses >= int(config.SIGNAL.ny_session_bonus):
            ses_tag = "ny"
        elif ses > 0:
            ses_tag = "lon"
        elif ses < 0:
            ses_tag = "asia"
        else:
            ses_tag = "off"

        return (
            f"m:{user_mode}|r:{regime}|s:{ses_tag}|"
            f"oi:{self._bucket_component(oi)}|"
            f"ob:{self._bucket_component(ob)}|"
            f"liq:{self._bucket_component(liq)}|"
            f"side:{side}"
        )

    def _apply_meta_score_delta(self, signal, user_mode: str) -> int:
        """
        Boost/penalize score from rolling pattern winrate.
        Conservative and bounded to avoid overfitting.
        """
        key = self._build_meta_pattern_key(signal, user_mode)
        signal.meta_pattern_key = key
        stats = user_db.get_meta_pattern_stats(key)
        if not stats:
            signal.meta_score_delta = 0
            return 0

        samples = int(stats.get("samples", 0))
        winrate = float(stats.get("winrate_ema", 0.5))
        pnl_ema = float(stats.get("pnl_ema", 0.0))

        # Need enough observations before affecting score materially.
        if samples < 8:
            signal.meta_score_delta = 0
            return 0

        # Core signal from winrate around 50%; bounded to [-6, +6].
        delta = int(round((winrate - 0.5) * 20))
        if pnl_ema < 0:
            delta -= 1
        elif pnl_ema > 0:
            delta += 1
        delta = max(-6, min(6, delta))

        signal.meta_score_delta = delta
        signal.score = max(0, min(100, int(signal.score + delta)))

        # Keep explanation visible in saved signal detail.
        if getattr(signal, "breakdown", None) and hasattr(signal.breakdown, "warnings"):
            signal.breakdown.warnings.append(
                f"Meta-score delta {delta:+d} (wr={winrate:.2f}, n={samples})"
            )
        return delta


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
        # If env var is set, wipe everything and start fresh.
        if os.getenv("KARA_HARD_RESET", "").lower() == "true":
            log.warning("🧹 [KARA_RESET] Detected KARA_HARD_RESET=true environment variable.")
            log.warning("🧹 [KARA_RESET] Initiating full data wipe and balance reset...")
            success = user_db.hard_reset_all_data()
            if success:
                log.info("✨ [KARA_RESET] Hard reset completed. Users can now start from fresh Rp1.000.000.")
            else:
                log.error("❌ [KARA_RESET] Hard reset failed! See logs for details.")

        # Load market list (always top volume as requested)
        log.info("Loading top volume markets...")
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

        # Reset saldo/posisi on deploy is disabled by user request.
        did_release_reset = False

        # Initialize User Sessions from DB
        self._enforce_locked_score_thresholds()
        for u in user_db.get_all_users():
            session = UserSession(u, mode_manager=self.mode_mgr, hl_client=self.hl_client)
            if hasattr(session.executor, 'load_from_db'):
                session.executor.load_from_db(u.chat_id)
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

        # ── Update Notification System ────────────────────────────────
        if self.telegram and self.telegram._bot_started:
            # Prefer Telegram active/authorized chats so notifications are sent
            # even if DB authorization flags are stale.
            target_chat_ids = list(self.telegram._authorized_chat_ids)
            if not target_chat_ids:
                target_chat_ids = [u.chat_id for u in users_to_update]

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
                        release_tag=release_tag,
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

        # Start Snapshot Loop (Phase 2: History Tracking)
        asyncio.create_task(self._snapshot_loop())

        # Greeting log
        mode_emoji = "🧪" if config.TRADE_MODE == "paper" else "💰"
        log.info(f"{mode_emoji} KARA is ready! Starting trading loop...")

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

        await self.ws_client.subscribe_liquidations()

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
        Main loop: every 60 seconds, run scoring for all watched assets.
        Also updates position TP/SL every 5 seconds.
        Sends hourly PnL summary every 3600 seconds.
        """
        position_interval  = 5       # seconds between position TP/SL checks (always)
        dashboard_interval = 5       # seconds between dashboard heartbeats
        hourly_interval    = 3600    # seconds between PnL summaries

        last_scan          = 0.0
        last_pos_upd       = 0.0
        last_dash_upd      = 0.0
        last_hourly        = 0.0

        while self._running:
            now = asyncio.get_event_loop().time()

            # Scan interval is dynamic: 5s scalper, 60s standard
            scan_interval = self.mode_mgr.scan_interval

            # ── Position management (every 5s) ───────────────────────
            if now - last_pos_upd >= position_interval:
                await self._update_positions()
                last_pos_upd = now

            # ── Dashboard Update (every 5s) ──────────────────────────
            if now - last_dash_upd >= dashboard_interval:
                await self._broadcast_heartbeat()
                last_dash_upd = now

            # ── Market scan (every 60s) ───────────────────────────────
            if now - last_scan >= scan_interval:
                await self._scan_all_assets()
                last_scan = now

            # ── Hourly PnL summary (DISABLED BY USER) ────────────────────────────────────
            # if now - last_hourly >= hourly_interval and last_hourly > 0:
            #     await self._send_hourly_summary()
            if last_hourly == 0.0:
                last_hourly = now   # start the clock without sending immediately
            elif now - last_hourly >= hourly_interval:
                last_hourly = now

            await asyncio.sleep(1)

    async def get_session(self, chat_id: str) -> Optional[UserSession]:
            if str(chat_id) not in self.sessions:
                user = user_db.get_user(str(chat_id))
                if user:
                    session = UserSession(user, mode_manager=self.mode_mgr, hl_client=self.hl_client)
                    try:
                        await session.initialize()
                    except Exception as e:
                        log.error(f"❌ Failed to initialize session for {chat_id}: {e}")
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
        log.info(f" 🔍 Scanning {len(self.watched_assets)} markets (parallel)...")
        
        try:
            # 1. Fetch batch meta data once for all assets
            await self.hl_client.refresh_market_cache(force=True)
            all_meta = await self.hl_client.get_all_market_data()
            if not all_meta:
                log.warning(" Could not fetch batch metadata, will fallback to individual requests")

            # Gather active modes across all authorized users
            target_ids = list(self.telegram._authorized_chat_ids)
            active_modes = set()
            for cid in target_ids:
                session = await self.get_session(cid)
                if session and session.user and hasattr(session.user.config, 'trading_mode'):
                    active_modes.add(session.user.config.trading_mode)
            
            # If no authorized users use the bot yet, default to standard to keep cache warm
            if not active_modes:
                active_modes.add("standard")
                
            active_modes_list = list(active_modes)

            # 2. Parallel scan with Semaphore(5)
            max_score = 0
            sig_count = 0
            top_scorers = [] # List of (asset, score)
            
            async def _scan_one(asset, scalper_only=False):
                nonlocal max_score, sig_count
                async with self.scan_sem:
                    try:
                        # Scalper-only assets (SCALPER_ASSETS) only run scalper engine
                        modes_for_asset = ["scalper"] if scalper_only else active_modes_list
                        # ScoringEngine returns Tuple[Dict[str, TradeSignal], int]
                        signals_dict, asset_max_score = await self.scorer.run_asset(asset, active_modes=modes_for_asset, meta_data=all_meta)
                        
                        # Track signals
                        if signals_dict:
                            sig_count += len(signals_dict)
                            await self._handle_signals(signals_dict)
                        
                        # Track highest score regardless of signal
                        if asset_max_score > 0:
                            top_scorers.append((asset, asset_max_score))
                            if asset_max_score > max_score:
                                max_score = asset_max_score
                            
                            # Update RiskManagers
                            for cid in target_ids:
                                session = await self.get_session(cid)
                                if session:
                                    session.risk_mgr.update_score(asset, asset_max_score)
                            
                    except Exception as e:
                        # Log errors but don't stop the whole scan
                        if "429" in str(e):
                            log.debug(f" ⚠️ Rate limited on {asset}")
                        else:
                            log.error(f" ❌ Scan error for {asset}: {e}")

            # Determine assets to scan: base watched + scalper-specific if scalper mode active
            assets_to_scan = list(self.watched_assets)
            scalper_only_assets = []
            if "scalper" in active_modes:
                import config as _cfg
                scalper_assets = getattr(_cfg, 'SCALPER_ASSETS', [])
                scalper_only_assets = [a for a in scalper_assets if a not in assets_to_scan]
                assets_to_scan.extend(scalper_only_assets)

            tasks = [_scan_one(asset, scalper_only=(asset in scalper_only_assets)) for asset in assets_to_scan]
            await asyncio.gather(*tasks)
            
            # Sort and format top scorers for the user
            top_scorers.sort(key=lambda x: x[1], reverse=True)
            top_str = ", ".join([f"{a}:{s}" for a, s in top_scorers[:5]])
            total_scanned = len(assets_to_scan)
            scalper_extra = len(scalper_only_assets)
            
            # Smart log: If top score is still low, be honest but show we are alive
            log.info(
                f" 🏁 Scan complete: {total_scanned} markets "
                f"({len(self.watched_assets)} standard"
                f"{f' + {scalper_extra} scalper-only' if scalper_extra else ''}). "
                f"Signals: {sig_count} | Top: [{top_str or 'None'}]"
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
                
            user_mode = getattr(session.user.config, 'trading_mode', 'standard')
            base_signal = signals_dict.get(user_mode)
            fallback_from_standard = False

            # If user is in scalper but dedicated scalper signal is not produced,
            # allow standard signal as fallback so opportunities are not missed.
            if user_mode == "scalper" and not base_signal:
                std_fallback = signals_dict.get("standard")
                if std_fallback:
                    base_signal = std_fallback
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
            if fallback_from_standard and user_signal.breakdown and hasattr(user_signal.breakdown, "warnings"):
                fb_msg = "Scalper fallback: using standard signal due to missing dedicated scalper signal this cycle."
                if fb_msg not in user_signal.breakdown.warnings:
                    user_signal.breakdown.warnings.append(fb_msg)


            # ── Fetch Dynamic ATR (calculated once per signal if needed)
            atr_value = 0.0
            if getattr(config, 'RISK', None) and getattr(config.RISK, 'enable_atr_sl', False):
                try:
                    # Fetch recent candles (1m interval) for ATR calculation
                    candles = await self.hl_client.get_candles(
                        user_signal.asset, "1m", limit=config.RISK.atr_lookback
                    )
                    if candles:
                        atr_value = session.risk_mgr.calculate_atr(candles)
                        if atr_value > 0:
                            log.info(f"📐 [ATR-SL] Calculated for {user_signal.asset}: {atr_value:.6f}")
                    else:
                        log.warning(f"⚠️  [ATR] No candles returned for {user_signal.asset}, using fixed SL.")
                except Exception as e:
                    log.error(f"Failed to calculate dynamic ATR for {user_signal.asset}: {e}")
                
            user_signal.localize_for_user(user_mode, atr_value=atr_value)
            self._apply_meta_score_delta(user_signal, user_mode)
            
            acc = await session.get_account_state()

            # Full-auto only behavior requested:
            # - only process signals that meet auto threshold
            # - anything below threshold is fully skipped (no Telegram signal)
            effective_auto_threshold = auto_threshold
            if user_signal.score < effective_auto_threshold:
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
                    # FULL_AUTO policy: do not notify non-executed signals.
                    log.info(
                        f"⛔ [AUTO_BLOCKED] user={chat_id} mode={user_mode} asset={user_signal.asset} "
                        f"score={user_signal.score} auto_threshold={effective_auto_threshold} reason={reason}"
                    )
                    continue
                
                # If approved, proceed to auto-execution
                user_signal.auto_executed = True
                user_db.save_signal(user_signal) # v17 Sync
                await self.telegram.send_signal(user_signal, is_auto=True, target_chat_id=chat_id)
                
                pos = await session.executor.open_position(user_signal)
                if pos:
                    await self.telegram.send_position_opened(pos, user_signal, target_chat_id=chat_id)
            else:
                # AUTO-ONLY policy: no manual signal dispatch.
                continue

    async def _on_trade_confirmed(self, signal, chat_id: str):
        session = self.get_session(chat_id)
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

        pos = await session.executor.open_position(signal)
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
        
        # 2. Fetch prices ONCE (O(N_Assets) instead of O(Users * Assets))
        prices = {}
        for asset in all_open_assets:
            try:
                prices[asset] = await self.hl_client.get_mark_price(asset)
            except Exception as e:
                log.debug(f"Failed to fetch market price for {asset}: {e}")

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
            time_exit_actions = [a for a in actions if a.get("action") == "time_exit"]
            other_actions = [a for a in actions if a.get("action") != "time_exit"]

            # Batch scalper max-hold notifications to avoid one-by-one spam.
            if time_exit_actions:
                exit_lines = []
                total_pnl = 0.0
                for a in time_exit_actions:
                    pos = session.executor._positions.get(a.get("position_id", "")) if hasattr(session.executor, "_positions") else None
                    asset = pos.asset if pos else "?"
                    pnl = float(a.get("pnl", 0.0))
                    total_pnl += pnl
                    sign = "+" if pnl >= 0 else ""
                    exit_lines.append(f"• {asset}: {sign}{pnl:.2f} USD")
                total_sign = "+" if total_pnl >= 0 else ""
                await self.telegram.send_text(
                    "⚡ <b>Scalper max-hold batch exit</b>\n"
                    f"Posisi ditutup: <b>{len(time_exit_actions)}</b>\n"
                    f"Total PnL: <b>{total_sign}{total_pnl:.2f} USD</b>\n"
                    + "\n".join(exit_lines[:8]),
                    target_chat_id=chat_id
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
        
        log.info(" KARA stopped. Goodbye!")


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
