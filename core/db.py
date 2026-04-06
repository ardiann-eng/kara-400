"""
KARA Bot - Simple JSON User Database
Ensures safe persistence of multi-user states across restarts.
Especially useful for Railway/Docker ephemeral environments.
"""

import os
import json
import logging
from typing import Dict, List, Optional
from threading import Lock

from models.schemas import User, UserConfig, BotMode

import config

log = logging.getLogger("kara.db")

class UserDB:
    def __init__(self, file_path: str = None):
        self.file_path = file_path or config.USER_DB_PATH
        self._lock = Lock()
        self.users: Dict[str, User] = {}
        
        # Ensure data dir exists
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        self.load()

    def load(self):
        with self._lock:
            if not os.path.exists(self.file_path):
                self.users = {}
                return
                
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for chat_id, u_data in data.items():
                        self.users[chat_id] = User(**u_data)
                log.info(f"Loaded {len(self.users)} users from database.")
            except Exception as e:
                log.error(f"Failed to load user DB: {e}")
                
    def save(self):
        with self._lock:
            try:
                with open(self.file_path, "w", encoding="utf-8") as f:
                    # serialize using pydantic
                    data = {k: v.dict() for k, v in self.users.items()}
                    # handle datetime parsing issues in basic json
                    json.dump(data, f, default=str, indent=2)
            except Exception as e:
                log.error(f"Failed to save user DB: {e}")

    def get_user(self, chat_id: str) -> Optional[User]:
        return self.users.get(str(chat_id))
        
    def get_all_users(self) -> List[User]:
        return list(self.users.values())

    def update_user(self, user: User):
        self.users[user.chat_id] = user
        self.save()

    def create_user(self, chat_id: str, username: str, init_usd: float) -> User:
        user = User(
            chat_id=str(chat_id),
            username=username,
            paper_balance_usd=init_usd,
            config=UserConfig(
                trading_mode="standard",
                bot_mode=BotMode.PAPER,
                risk_pct=0.02,
                max_positions=5
            )
        )
        self.users[str(chat_id)] = user
        self.save()
        return user

# Global instance
user_db = UserDB()
