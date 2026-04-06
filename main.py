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

        # ModeManager — single source of truth for active strategy mode
        self.mode_mgr   = mode_manager

        # Scoring engine (stateless regarding user risk)
        self.scorer     = ScoringEngine(self.hl_client, self.cache, None, self.mode_mgr)

        # Multi-user session store (chat_id -> UserSession)
        self.sessions: Dict[str, UserSession] = {}

        # Notification
        self.telegram   = KaraTelegram(on_confirm=self._on_trade_confirmed)
        self.telegram.bot_app      = self  # allow telegram to access user sessions
        self.telegram.hl_client    = self.hl_client
        self.telegram.mode_manager = self.mode_mgr  # inject for /scalper /standard

        # Dynamic market list (loaded at startup)
        self.watched_assets: List[str] = []

        self._running   = False

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

        # Load market list (always top volume as requested)
        log.info("Loading top volume markets...")
        self.watched_assets = await self.hl_client.get_top_volume_markets(top_n=100)

        log.info(f"   Markets ({len(self.watched_assets)}): {', '.join(self.watched_assets[:15])}{'...' if len(self.watched_assets) > 15 else ''}")
        log.info(f"   Full-auto: {config.FULL_AUTO}")
        log.info("=" * 60)

        # Setup WebSocket subscriptions
        await self._setup_websocket()

        # Initialize User Sessions from DB
        for u in user_db.get_all_users():
            self.sessions[u.chat_id] = UserSession(u, mode_manager=self.mode_mgr, hl_client=self.hl_client)
        log.info(f"Loaded {len(self.sessions)} user sessions.")

        # Inject into dashboard (passing sessions registry for multi-user support)
        init_dashboard(self.sessions, self.telegram, self.mode_mgr)

        # Start Telegram (optional, errors are graceful)
        try:
            await self.telegram.start()
        except Exception as e:
            log.warning(f"  Telegram startup failed: {e}")
            log.warning("   Bot will continue without Telegram notifications")

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
                "session_bonus": SIGNAL.ny_session_bonus + SIGNAL.london_session_bonus,
                "regime": MarketRegime.TRENDING
            })
            log.info(f"   Realistic params → Score: {realistic['score']}/100 ✓ (Expected: 45-65)")
            
            # 2. Strong
            strong = await self.scorer.simulate_score({
                "funding_rate": 0.0004 / 3,
                "oi_change_1h": 0.025,
                "cascade_risk": 0.4,
                "imbalance": 0.65,
                "vwap_dev": -0.004,
                "session_bonus": SIGNAL.ny_session_bonus,
                "regime": MarketRegime.TRENDING
            })
            log.info(f"   Strong signal    → Score: {strong['score']}/100 ✓ (Expected: 72-85)")
        except Exception as e:
            log.error(f"   Calibration test failed: {e}")

        self._running = True

        # Greeting log
        mode_emoji = "" if MODE == "paper" else ""
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

        # Subscribe to each watched asset
        for asset in self.watched_assets:
            await self.ws_client.subscribe_orderbook(asset)
            await self.ws_client.subscribe_trades(asset)
            await self.ws_client.subscribe_funding(asset)

        await self.ws_client.subscribe_liquidations()

        if config.WALLET_ADDRESS:
            await self.ws_client.subscribe_user_events(config.WALLET_ADDRESS)

        log.info(f" WS subscribed: {', '.join(self.watched_assets)}")

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

            # ── Hourly PnL summary ────────────────────────────────────
            if now - last_hourly >= hourly_interval and last_hourly > 0:
                await self._send_hourly_summary()
            if last_hourly == 0.0:
                last_hourly = now   # start the clock without sending immediately
            elif now - last_hourly >= hourly_interval:
                last_hourly = now

            await asyncio.sleep(1)

    def get_session(self, chat_id: str) -> Optional[UserSession]:
        if str(chat_id) not in self.sessions:
            user = user_db.get_user(str(chat_id))
            if user:
                self.sessions[str(chat_id)] = UserSession(user, mode_manager=self.mode_mgr, hl_client=self.hl_client)
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
            acc = session.get_account_state()
            
            # 2. Broadcast account summary
            await broadcast({
                "type": "account_update",
                **acc.dict()
            })
            
            # 3. Broadcast open positions
            await broadcast({
                "type": "positions_update",
                "positions": [p.dict() for p in session.executor.open_positions]
            })
            
        except Exception as e:
            # Silent fail for heartbeat to avoid loop crashes
            pass

    async def _scan_all_assets(self):
        """Run scoring engine for all watched assets."""
        try:
            log.info(f"Scanning {len(self.watched_assets)} markets (batch)...")
            scan_errors = 0

            # 1. Fetch batch meta data once for all assets
            await self.hl_client.refresh_market_cache(force=True)
            all_meta = await self.hl_client.get_all_market_data()
            if not all_meta:
                log.warning("Could not fetch batch metadata, will fallback to individual requests")

            for asset in self.watched_assets:
                # In multi-user mode, we scan all watched assets.
                # Per-user risk distancing/pyramiding is handled in _handle_signal.
                try:
                    signal = await self.scorer.run_asset(asset, meta_data=all_meta)
                    if signal:
                        await self._handle_signal(signal)
                    scan_errors = 0  # reset on success
                except Exception as e:
                    scan_errors += 1
                    if scan_errors <= 3:  # log first few with details
                        log.error(f"❌ Scan error for {asset}: {e}", exc_info=True)
                    else:
                        log.warning(f"Scan error for {asset}: {type(e).__name__}")
                await asyncio.sleep(0.4)
        except Exception as e:
            log.error(f"Scan loop error: {e}")

    async def _handle_signal(self, signal):
        """Handle a new trade signal and broadcast to all authorized user sessions."""
        # 1. Loop through all authorized users (from Telegram state)
        # This ensures users in config.TELEGRAM_CHAT_ID get signals even before hitting /start
        target_ids = list(self.telegram._authorized_chat_ids)
        if not target_ids:
            log.warning("No authorized users found for signal broadcast.")
            return

        for chat_id in target_ids:
            # Get or create session
            session = self.get_session(chat_id)
            if not session:
                log.info(f"Initializing auto-session for authorized user: {chat_id}")
                # Use a default username for auto-sessions
                user = user_db.create_user(chat_id, "Master", init_usd=config.PAPER_BALANCE_USD)
                session = self.get_session(chat_id)

            # Copy signal to avoid shared reference issues
            user_signal = signal.model_copy()
            user_signal.localize_for_user(session.user.config.trading_mode)
            
            acc = session.get_account_state()
            
            # Enrich signal with position sizing for THIS user
            size_usd, contracts = session.risk_mgr.calculate_position_size(
                user_signal, acc.total_equity
            )
            user_signal.suggested_size_usd   = size_usd
            user_signal.suggested_contracts  = contracts

            # ⚡ PRE-TRADE VALIDATION (Before Notification)
            # We only send signals if we can actually trade them (FULL_AUTO)
            if config.FULL_AUTO and user_signal.score >= config.SIGNAL.min_score_to_auto_trade and not getattr(user_signal, 'is_pyramid', False):
                approved, reason = session.risk_mgr.pre_trade_check(
                    user_signal, acc, session.executor.open_positions
                )
                
                if not approved:
                    log.info(f"🔇 Sinyal {user_signal.asset} dibisukan (Muted): {reason}")
                    continue  # SILENT SKIP
                
                # If approved, proceed to auto-execution
                user_signal.auto_executed = True
                await self.telegram.send_signal(user_signal, is_auto=True, target_chat_id=chat_id)
                
                pos = await session.executor.open_position(user_signal)
                if pos:
                    await self.telegram.send_position_opened(pos, user_signal, target_chat_id=chat_id)
            else:
                # Manual mode: always send so the user can decide
                await self.telegram.send_signal(user_signal, target_chat_id=chat_id)

    async def _on_trade_confirmed(self, signal, chat_id: str):
        session = self.get_session(chat_id)
        if not session: return
        
        log.info(f" User {chat_id} confirmed trade: {signal.asset}")
        pos = await session.executor.open_position(signal)
        if pos:
            await self.telegram.send_position_opened(pos, signal, target_chat_id=chat_id)
        else:
            await self.telegram.send_text(f"❌ Gagal mengeksekusi <b>{signal.asset}</b>.", target_chat_id=chat_id)

    async def _update_positions(self):
        """Update unrealized PnL and check TP/SL for all users."""
        for chat_id, session in self.sessions.items():
            if not hasattr(session.executor, 'open_positions'):
                continue
            
            # ── Daily Reset Check ───────────────────────────────────────
            # This handles the UTC midnight transition for each user
            acc = session.get_account_state()
            if session.risk_mgr.reset_daily(acc.total_equity):
                pos_count = len(session.executor.open_positions)
                await self.telegram.send_daily_report(acc, pos_count, target_chat_id=chat_id)
                log.info(f"📬 Daily report sent to {chat_id}")
            positions = session.executor.open_positions
            if not positions:
                continue

            prices = {}
            for pos in positions:
                if pos.asset not in prices:
                    try:
                        prices[pos.asset] = await self.hl_client.get_mark_price(pos.asset)
                    except:
                        pass

            actions = await session.executor.update_positions(prices)
            for action in actions:
                await self.telegram.send_position_event(action, prices, target_chat_id=chat_id)
            
            # Save user state after positional update to persist PnL changes
            # We sync balance to user DB periodically
            session.user.paper_balance_usd = session.get_account_state().total_equity
            user_db.update_user(session.user)

    async def _send_hourly_summary(self):
        """Send hourly PnL summary to all users."""
        for chat_id, session in self.sessions.items():
            try:
                acc = session.get_account_state()
                open_count = len(session.executor.open_positions)
                await self.telegram.send_hourly_summary(acc, open_count, target_chat_id=chat_id)
            except Exception as e:
                log.debug(f"Hourly summary error for {chat_id}: {e}")

    async def _on_user_event(self, data):
        """Handle live user events (fills, funding, etc.) from WS."""
        log.debug(f"User event: {data}")
        # In live mode, sync position fills here

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
        except: pass
        
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

    try:
        await bot.start()
    except Exception as e:
        log.error(f" Initialization failed: {e}")
        log.error("\n📋 Troubleshooting:")
        log.error("  1. Check your .env file exists and is configured")
        log.error("  2. Run: python test_components.py")
        log.error("  3. Run: python test_connection.py")
        log.error("  4. View logs: tail -f kara.log")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    loop = asyncio.get_running_loop()

    # Graceful shutdown on SIGINT / SIGTERM (Unix/Linux only, not Windows)
    # Windows uses Ctrl+C which asyncio.run() handles automatically
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(
                    sig,
                    lambda: asyncio.create_task(bot.stop())
                )
            except Exception as e:
                log.warning(f"Could not setup signal handler: {e}")

    # Run trading loop + dashboard concurrently
    try:
        await asyncio.gather(
            bot.run_trading_loop(),
            run_dashboard(),
        )
    except Exception as e:
        log.error(f" Runtime error: {e}")
        import traceback
        traceback.print_exc()
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
