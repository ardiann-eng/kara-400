import logging
import time


log = logging.getLogger("kara.main")


async def open_and_notify_auto_position(executor, telegram, signal, chat_id: str):
    """Notify only after executor confirms that a position exists."""
    started_at = time.monotonic()
    position = await executor.open_position(signal)
    latency_ms = (time.monotonic() - started_at) * 1000
    log.info(
        "[EXEC] %s %s score=%s latency=%.0fms",
        signal.asset,
        signal.side.value.upper(),
        signal.score,
        latency_ms,
    )
    if not position:
        return None
    await telegram.send_signal(signal, is_auto=True, target_chat_id=chat_id)
    await telegram.send_position_opened(position, signal, target_chat_id=chat_id)
    return position
