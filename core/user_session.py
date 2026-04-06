"""
KARA Bot - User Session
Encapsulates all execution and risk state for a single user.
"""

from typing import Optional
from models.schemas import User, BotMode
from risk.risk_manager import RiskManager
from execution.paper_executor import PaperExecutor
# Assuming live executor import would happen here, deferred for simplicity in this file for now

class UserSession:
    def __init__(self, user: User, mode_manager=None, hl_client=None):
        self.user = user
        self.mode_manager = mode_manager
        self.hl_client = hl_client
        
        self.risk_mgr = RiskManager(mode_manager=self.mode_manager)
        
        # Hydrate initial balances
        self.risk_mgr.reset_daily(self.user.paper_balance_usd)
        self.risk_mgr._peak_balance = self.user.paper_balance_usd
        
        # Instantiate executor
        if self.user.config.bot_mode == BotMode.PAPER:
            self.executor = PaperExecutor(self.risk_mgr, initial_balance=self.user.paper_balance_usd)
        else:
            # Placeholder for Live mode to be injected later/handled properly
            self.executor = PaperExecutor(self.risk_mgr, initial_balance=self.user.paper_balance_usd)
            
    def get_account_state(self):
        return self.executor.get_account_state()
