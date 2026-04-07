"""
KARA Bot - User Session
Encapsulates all execution and risk state for a single user.
"""

from typing import Optional
import logging
from models.schemas import User, BotMode
from risk.risk_manager import RiskManager
from execution.paper_executor import PaperExecutor

log = logging.getLogger("kara.user_session")

class UserSession:
    def __init__(self, user: User, mode_manager=None, hl_client=None):
        self.user = user
        self.hl_client = hl_client  # This is the global one for read-only if needed
        
        self.risk_mgr = RiskManager(chat_id=self.user.chat_id)
        
        # Hydrate initial balances
        self.risk_mgr.reset_daily(self.user.paper_balance_usd)
        self.risk_mgr._peak_balance = self.user.paper_balance_usd
        
        # Instantiate executor
        if self.user.config.bot_mode == BotMode.PAPER:
            self.executor = PaperExecutor(self.risk_mgr, initial_balance=self.user.paper_balance_usd, chat_id=self.user.chat_id)
        elif self.user.config.bot_mode == BotMode.LIVE:
            from execution.live_executor import LiveExecutor
            from data.hyperliquid_client import HyperliquidClient
            
            if not self.user.hl_agent_secret or not self.user.hl_agent_address:
                log.error(f"User {self.user.chat_id} is in LIVE mode but missing Agent Secret/Address. Falling back to PAPER.")
                self.executor = PaperExecutor(self.risk_mgr, initial_balance=self.user.paper_balance_usd, chat_id=self.user.chat_id)
            else:
                user_client = HyperliquidClient(
                    wallet_address=self.user.hl_agent_address,
                    private_key=self.user.hl_agent_secret
                )
                self.executor = LiveExecutor(user_client, self.risk_mgr)
        else:
            self.executor = PaperExecutor(self.risk_mgr, initial_balance=self.user.paper_balance_usd, chat_id=self.user.chat_id)
            
    def get_account_state(self):
        return self.executor.get_account_state()
