"""
Log deduplication helpers to suppress identical API error floods.
Prevents 100 parallel asset workers from emitting 100 identical error lines.
"""

import time
import logging
from typing import Dict, Tuple

_last_api_error: Dict[str, Tuple[str, int, float]] = {}
_API_ERROR_SUPPRESS_SECONDS = 60


def log_api_error_once(logger: logging.Logger, endpoint: str, message: str) -> None:
    """
    Log an API error once per 60s window; suppress duplicates and summarise on expiry.

    Suppression key is endpoint + first 30 chars of message, so different errors on
    the same endpoint are tracked independently.
    """
    now = time.monotonic()
    key = f"{endpoint}:{message[:30]}"
    last = _last_api_error.get(key)

    if last:
        last_msg, count, first_ts = last
        if now - first_ts < _API_ERROR_SUPPRESS_SECONDS:
            _last_api_error[key] = (last_msg, count + 1, first_ts)
            return
        # Window expired — log summary then fall through to log the fresh error
        if count > 1:
            logger.warning(
                f"[API] {endpoint}: {last_msg} (repeated {count}x in last 60s)"
            )

    logger.error(f"[API] {endpoint}: {message}")
    _last_api_error[key] = (message, 1, now)
