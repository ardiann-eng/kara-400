"""
KARA Bot - User Session
Encapsulates all execution and risk state for a single user.

Executor selection:
  bot_mode = PAPER  → PaperExecutor (always, regardless of EXECUTION_EXCHANGE)
  bot_mode = LIVE   → branch by config.EXECUTION_EXCHANGE:
      "bitget"      → BitgetExecutor (butuh user.bitget_authorized + credentials)
      "hyperliquid" → LiveExecutor   (butuh user.wallet_authorized + agent secret)
  Jika credentials kurang, fallback ke PaperExecutor untuk safety.
"""

from typing import Optional
import logging
import config
from models.schemas import User, BotMode
from risk.risk_manager import RiskManager
from execution.paper_executor import PaperExecutor

log = logging.getLogger("kara.user_session")


class UserSession:
    def __init__(
        self,
        user: User,
        mode_manager=None,
        hl_client=None,
        bitget_client=None,
        symbol_registry=None,
        price_bridge=None,
        bybit_client=None,
    ):
        self.user = user
        self.hl_client = hl_client       # global HL client (read-only context)
        self.bitget_client = bitget_client
        self.symbol_registry = symbol_registry
        self.price_bridge = price_bridge
        self.bybit_client = bybit_client

        self.risk_mgr = RiskManager(chat_id=self.user.chat_id)
        self.risk_mgr.reset_daily(self.user.paper_balance_usd)
        self.risk_mgr._peak_balance = self.user.paper_balance_usd

        from data.ws_client import market_cache
        self.user_client = None  # set hanya untuk HL live mode

        # ── Resolve user-config leverage cap ──────────────────────────
        # [BUG FIX 2026-05-17] Pass user_max_leverage ke PaperExecutor supaya
        # leverage paper konsisten dengan setting Telegram (/settings).
        # Tanpa ini, paper executor pakai default scoring engine (15x) bahkan
        # kalau user set 5x — leverage user config baru kena triple-cap di
        # risk_manager, tapi log/notif sudah keburu pakai signal default.
        cfg = self.user.config
        if cfg.trading_mode == "scalper":
            _user_lev_cap = cfg.scl_max_leverage
        else:
            _user_lev_cap = cfg.std_max_leverage

        # ── Executor selection ────────────────────────────────────────
        self.executor = None
        if self.user.config.bot_mode == BotMode.PAPER:
            self.executor = PaperExecutor(
                self.risk_mgr,
                initial_balance=self.user.paper_balance_usd,
                chat_id=self.user.chat_id,
                market_cache=market_cache,
                user_max_leverage=_user_lev_cap,
            )
        elif self.user.config.bot_mode == BotMode.LIVE:
            self.executor = self._build_live_executor(market_cache)
        else:
            self.executor = PaperExecutor(
                self.risk_mgr,
                initial_balance=self.user.paper_balance_usd,
                chat_id=self.user.chat_id,
                market_cache=market_cache,
                user_max_leverage=_user_lev_cap,
            )

    def _build_live_executor(self, market_cache):
        """Pilih live executor berdasarkan EXECUTION_EXCHANGE + user credentials."""
        exec_exchange = (config.EXECUTION_EXCHANGE or "hyperliquid").lower()

        if exec_exchange == "bitget":
            return self._build_bitget_executor() or self._fallback_paper(market_cache, reason="bitget_not_ready")
        elif exec_exchange == "bybit":
            return self._build_bybit_executor() or self._fallback_paper(market_cache, reason="bybit_not_ready")

        # Default: Hyperliquid live
        return self._build_hl_executor() or self._fallback_paper(market_cache, reason="hl_not_ready")

    def _build_bitget_executor(self):
        if not self.bitget_client or not self.symbol_registry or not self.price_bridge:
            log.error(
                f"[SESSION {self.user.chat_id}] EXECUTION_EXCHANGE=bitget tapi bitget_client/registry/bridge "
                f"belum di-inject. Fallback ke paper."
            )
            return None

        # Pilih credentials: per-user dulu, fallback ke env global
        api_key    = self.user.bitget_api_key or config.BITGET_API_KEY
        api_secret = self.user.bitget_api_secret or config.BITGET_SECRET_KEY
        passphrase = self.user.bitget_passphrase or config.BITGET_PASSPHRASE

        if not (api_key and api_secret and passphrase):
            log.error(
                f"[SESSION {self.user.chat_id}] Bitget credentials kosong. "
                f"User wajib /live → setup Bitget. Fallback ke paper."
            )
            return None

        # Lightweight clone: share HTTP pool dengan global Bitget client
        user_bg = self.bitget_client.with_credentials(api_key, api_secret, passphrase)

        # Resolve user leverage cap
        cfg = self.user.config
        if cfg.bitget_max_leverage > 0:
            user_max_lev = cfg.bitget_max_leverage
        elif cfg.trading_mode == "scalper":
            user_max_lev = cfg.scl_max_leverage
        else:
            user_max_lev = cfg.std_max_leverage

        from execution.bitget_executor import BitgetExecutor
        executor = BitgetExecutor(
            chat_id=self.user.chat_id,
            bitget_client=user_bg,
            symbol_registry=self.symbol_registry,
            price_bridge=self.price_bridge,
            risk_manager=self.risk_mgr,
            user_max_leverage=user_max_lev,
        )
        log.info(
            f"[SESSION {self.user.chat_id}] BitgetExecutor active (user_max_lev={user_max_lev}x)"
        )
        return executor

    def _build_hl_executor(self):
        from execution.live_executor import LiveExecutor
        from data.hyperliquid_client import HyperliquidClient

        if not self.user.hl_agent_secret or not self.user.hl_agent_address:
            log.error(
                f"[SESSION {self.user.chat_id}] LIVE/HL tapi Agent Secret/Address kosong."
            )
            return None

        self.user_client = HyperliquidClient(
            wallet_address=self.user.hl_agent_address,
            private_key=self.user.hl_agent_secret,
        )
        return LiveExecutor(self.user.chat_id, self.user_client, self.risk_mgr)

    def _build_bybit_executor(self):
        if not self.bybit_client:
            log.error(f"[SESSION {self.user.chat_id}] EXECUTION_EXCHANGE=bybit tapi bybit_client belum di-inject. Fallback ke paper.")
            return None

        api_key    = self.user.bitget_api_key or config.BYBIT_API_KEY
        api_secret = self.user.bitget_api_secret or config.BYBIT_SECRET_KEY

        if not (api_key and api_secret):
            log.error(f"[SESSION {self.user.chat_id}] Bybit credentials kosong. Fallback ke paper.")
            return None

        user_bybit = self.bybit_client.with_credentials(api_key, api_secret)

        cfg = self.user.config
        user_max_lev = cfg.scl_max_leverage if cfg.trading_mode == "scalper" else cfg.std_max_leverage

        from execution.bybit_executor import BybitExecutor
        executor = BybitExecutor(
            chat_id=self.user.chat_id,
            bybit_client=user_bybit,
            risk_manager=self.risk_mgr,
            user_max_leverage=user_max_lev,
        )
        log.info(f"[SESSION {self.user.chat_id}] BybitExecutor active (user_max_lev={user_max_lev}x)")
        return executor

    def _fallback_paper(self, market_cache, reason: str):
        log.warning(f"[SESSION {self.user.chat_id}] Fallback to paper ({reason})")
        cfg = self.user.config
        _user_lev_cap = cfg.scl_max_leverage if cfg.trading_mode == "scalper" else cfg.std_max_leverage
        return PaperExecutor(
            self.risk_mgr,
            initial_balance=self.user.paper_balance_usd,
            chat_id=self.user.chat_id,
            market_cache=market_cache,
            user_max_leverage=_user_lev_cap,
        )

    async def initialize(self):
        """Async setup: connect HL client, sync chain state."""
        # Hyperliquid path
        if self.user_client:
            await self.user_client.connect()
            log.info(f"✓ UserSession {self.user.chat_id}: HL client connected.")

        # Position sync — any executor that implements it (HL + Bitget)
        if hasattr(self.executor, 'sync_positions_from_chain'):
            try:
                await self.executor.sync_positions_from_chain()
            except Exception as e:
                log.warning(f"[SESSION {self.user.chat_id}] sync_positions failed: {e}")

    async def get_account_state(self):
        return await self.executor.get_account_state()
