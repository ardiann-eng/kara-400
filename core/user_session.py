"""
KARA Bot - User Session
Encapsulates all execution and risk state for a single user.
"""

from typing import Optional
import logging
from models.schemas import User, BotMode, ExecutionEnvironment
from risk.risk_manager import RiskManager

log = logging.getLogger("kara.user_session")

class UserSession:
    def __init__(
        self,
        user: User,
        mode_manager=None,
        hl_client=None,
        bybit_client=None,
        bybit_registry=None,
        persistence=None,
        alert_sink=None,
    ):
        self.user = user
        self.hl_client = hl_client  # This is the global one for read-only if needed
        self.bybit_client = bybit_client
        
        self.risk_mgr = RiskManager(chat_id=self.user.chat_id)
        
        # Hydrate initial balances
        self.risk_mgr.reset_daily(self.user.paper_balance_usd)
        self.risk_mgr._peak_balance = self.user.paper_balance_usd
        
        # Instantiate executor
        if self.user.config.bot_mode == BotMode.PAPER:
            from execution.paper_executor import PaperExecutor
            self.executor = PaperExecutor(self.risk_mgr, initial_balance=self.user.paper_balance_usd, chat_id=self.user.chat_id)
        elif self.user.config.bot_mode == BotMode.LIVE:
            if not bybit_registry:
                raise RuntimeError("Bybit live dependencies are unavailable")
            if not (
                self.user.bybit_authorized
                and self.user.bybit_api_key
                and self.user.bybit_api_secret
            ):
                raise RuntimeError("User Bybit credentials are not authorized")
            from execution.bybit_executor import BybitExecutor
            from execution.price_bridge import HyperliquidBybitPriceBridge
            from data.bybit_client import BybitClient
            from data.bybit_private_ws import BybitPrivateWebSocket
            from core.bybit_observability import BybitAlertManager, BybitTelemetry
            from execution.live_risk_gate import BybitLiveRiskGate, LiveRiskLimits
            import config

            environment = self.user.bybit_environment
            # Old JSON records have an explicit bybit_testnet boolean but no
            # environment field. Use it only as a runtime compatibility view;
            # never overwrite persisted credential environment by inference.
            if environment == ExecutionEnvironment.PAPER:
                environment = (
                    ExecutionEnvironment.LEGACY_TESTNET
                    if self.user.bybit_testnet else ExecutionEnvironment.MAINNET
                )
            if environment not in (
                ExecutionEnvironment.DEMO,
                ExecutionEnvironment.MAINNET,
                ExecutionEnvironment.LEGACY_TESTNET,
            ):
                raise RuntimeError(
                    "User Bybit credential environment is not executable"
                )
            if environment == ExecutionEnvironment.MAINNET and config.BYBIT_TESTNET:
                raise RuntimeError("User Bybit credential environment does not match server BYBIT_TESTNET")
            if environment == ExecutionEnvironment.LEGACY_TESTNET and not config.BYBIT_TESTNET:
                raise RuntimeError("User Bybit credential environment does not match server BYBIT_TESTNET")

            self.bybit_telemetry = BybitTelemetry(
                environment=f"BYBIT {environment.value.upper()}"
            )
            self.bybit_alerts = BybitAlertManager(alert_sink)
            live_risk_gate = BybitLiveRiskGate(LiveRiskLimits(
                max_leverage=config.BYBIT_LIVE_MAX_LEVERAGE,
                max_positions=config.BYBIT_LIVE_MAX_POSITIONS,
                max_risk_per_trade_pct=config.BYBIT_LIVE_MAX_RISK_PER_TRADE_PCT,
                max_total_open_risk_pct=config.BYBIT_LIVE_MAX_TOTAL_RISK_PCT,
                max_symbol_notional_pct=config.BYBIT_LIVE_MAX_SYMBOL_NOTIONAL_PCT,
                max_total_notional_pct=config.BYBIT_LIVE_MAX_TOTAL_NOTIONAL_PCT,
                max_signal_age_s=config.BYBIT_LIVE_MAX_SIGNAL_AGE_S,
                max_quote_age_s=config.BYBIT_LIVE_MAX_QUOTE_AGE_S,
                max_spread_pct=config.BYBIT_LIVE_MAX_SPREAD_PCT,
                max_slippage_pct=config.BYBIT_MAX_SLIPPAGE_PCT,
                min_depth_ratio=config.BYBIT_LIVE_MIN_DEPTH_RATIO,
            ))

            self.bybit_client = BybitClient(
                api_key=self.user.bybit_api_key,
                api_secret=self.user.bybit_api_secret,
                testnet=environment == ExecutionEnvironment.LEGACY_TESTNET,
                demo=environment == ExecutionEnvironment.DEMO,
                recv_window=config.BYBIT_RECV_WINDOW,
                telemetry=self.bybit_telemetry,
            )
            self.bybit_ws = BybitPrivateWebSocket(
                api_key=self.user.bybit_api_key,
                api_secret=self.user.bybit_api_secret,
                testnet=environment == ExecutionEnvironment.LEGACY_TESTNET,
                demo=environment == ExecutionEnvironment.DEMO,
                telemetry=self.bybit_telemetry,
            )

            self.executor = BybitExecutor(
                chat_id=self.user.chat_id,
                client=self.bybit_client,
                risk_manager=self.risk_mgr,
                symbol_registry=bybit_registry,
                price_bridge=HyperliquidBybitPriceBridge(
                    config.BYBIT_MAX_PRICE_GAP_PCT
                ),
                persistence=persistence,
                private_ws=self.bybit_ws,
                telemetry=self.bybit_telemetry,
                alerts=self.bybit_alerts,
                live_risk_gate=live_risk_gate,
                user=self.user,
            )
            self.bybit_ws.on_reconnect = self._reconcile_after_ws_reconnect
            self.bybit_ws.on_state_event = self._handle_bybit_state_event
        else:
            from execution.paper_executor import PaperExecutor
            self.executor = PaperExecutor(self.risk_mgr, initial_balance=self.user.paper_balance_usd, chat_id=self.user.chat_id)
            
    async def initialize(self):
        """Perform executor-specific asynchronous initialization."""
        if self.user.config.bot_mode == BotMode.LIVE:
            await self.bybit_client.connect()
            await self.bybit_client.sync_clock()
            await self.bybit_client.load_instruments()
            self.executor.registry = self.bybit_client.symbol_registry
            await self.bybit_ws.start()
            self.executor.load_persisted_positions()
            await self.executor.reconcile_if_due(force=True)
            if self.executor.open_positions:
                await self.bybit_alerts.emit(
                    "startup_exchange_positions",
                    "WARNING BYBIT: startup menemukan posisi exchange aktif: "
                    + ", ".join(
                        sorted(position.asset for position in self.executor.open_positions)
                    ),
                )

    async def _reconcile_after_ws_reconnect(self):
        await self.executor.reconcile_if_due(force=True)

    async def _handle_bybit_state_event(self, topic: str, row: dict):
        if topic in ("execution", "position", "wallet"):
            try:
                await self.executor.reconcile_if_due(force=True)
            except Exception:
                # WS callbacks run as detached tasks. Log failure without leaving
                # an unhandled task exception that hides the actual lifecycle error.
                log.exception("Bybit WS state-event reconciliation failed")

    async def get_account_state(self):
        return await self.executor.get_account_state()

    def bybit_status(self):
        telemetry = getattr(self, "bybit_telemetry", None)
        if not telemetry:
            return None
        if getattr(self, "bybit_ws", None):
            telemetry.ws_connected = self.bybit_ws.connected
            telemetry.ws_stale = self.bybit_ws.stale
        if getattr(self, "executor", None):
            telemetry.circuit_open = self.executor.circuit_open
            telemetry.circuit_remaining_s = max(
                0.0, self.executor._circuit_open_until - __import__("time").monotonic()
            )
        snapshot = telemetry.snapshot()
        if self.user.bybit_environment in (
            ExecutionEnvironment.DEMO, ExecutionEnvironment.MAINNET,
        ):
            snapshot["capital_allocation_idr"] = self.user.capital_allocation_idr
            snapshot["capital_allocation_usd"] = self.user.capital_allocation_usd
            snapshot["capital_fx_rate"] = self.user.capital_fx_rate
        gate = getattr(getattr(self, "executor", None), "live_risk_gate", None)
        if gate:
            limits = gate.limits
            snapshot["live_risk_limits"] = {
                "max_leverage": limits.max_leverage,
                "max_positions": limits.max_positions,
                "max_risk_per_trade_pct": limits.max_risk_per_trade_pct,
                "max_total_open_risk_pct": limits.max_total_open_risk_pct,
                "max_symbol_notional_pct": limits.max_symbol_notional_pct,
                "max_total_notional_pct": limits.max_total_notional_pct,
                "max_signal_age_s": limits.max_signal_age_s,
                "max_quote_age_s": limits.max_quote_age_s,
                "max_spread_pct": limits.max_spread_pct,
                "max_slippage_pct": limits.max_slippage_pct,
                "min_depth_ratio": limits.min_depth_ratio,
            }
        return snapshot
