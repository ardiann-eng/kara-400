"""
KARA / CONNOR-15 Bot — Professional Colored Logging System
Uses Rich + Loguru for beautiful, structured, color-coded logs
Works on local terminal AND Railway cloud console

Usage:
    from core.logger import get_logger
    log = get_logger(__name__)

    log.info("Starting bot")
    log.success("Trade executed", extra={"trade_id": "tx_123"})
    log.warning("High drawdown detected", drawdown=3.2)
    log.error("Order failed", order_id=456)
"""

import logging
import sys
import os
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any

# Check if running in Railway or local
IS_RAILWAY = os.getenv("RAILWAY_ENVIRONMENT") is not None
IS_LOCAL = not IS_RAILWAY

# Try to import rich for colored output
try:
    from rich.logging import RichHandler
    from rich.console import Console
    from rich.theme import Theme
    HAS_RICH = True
except ImportError:
    HAS_RICH = False
    print("[WARNING] Rich not installed - using basic logging. Run: pip install rich")

# Try to import loguru for structured logging
try:
    from loguru import logger as loguru_logger
    HAS_LOGURU = True
except ImportError:
    HAS_LOGURU = False
    print("[WARNING] Loguru not installed. Run: pip install loguru")


# ══════════════════════════════════════════════════════════
# RICH THEME (Colors)
# ══════════════════════════════════════════════════════════

THEME = Theme({
    "info": "cyan",
    "success": "green",
    "warning": "yellow",
    "error": "red",
    "critical": "bold red on dark_red",
    "debug": "blue",
    "module": "bright_black",
    "time": "dim white",
    "emoji": "white",
})


# ══════════════════════════════════════════════════════════
# LOG LEVEL EMOJIS & COLORS
# ══════════════════════════════════════════════════════════

LOG_EMOJI = {
    "DEBUG": "🔍",
    "INFO": "ℹ️",
    "SUCCESS": "✅",
    "WARNING": "⚠️",
    "ERROR": "❌",
    "CRITICAL": "🚨",
}

# ANSI Color codes for fallback (no Rich)
ANSI_COLORS = {
    "RESET": "\033[0m",
    "CYAN": "\033[96m",
    "GREEN": "\033[92m",
    "YELLOW": "\033[93m",
    "RED": "\033[91m",
    "BOLD_RED": "\033[1;91m",
    "BLUE": "\033[94m",
    "DIM": "\033[2m",
    "BRIGHT_BLACK": "\033[90m",
}


# ══════════════════════════════════════════════════════════
# CUSTOM RICH HANDLER
# ══════════════════════════════════════════════════════════

class ColoredFormatter(logging.Formatter):
    """Custom formatter with colors (for non-Rich fallback)."""

    def format(self, record: logging.LogRecord) -> str:
        # Get colors
        if record.levelno >= logging.CRITICAL:
            level_color = ANSI_COLORS["BOLD_RED"]
            emoji = LOG_EMOJI["CRITICAL"]
        elif record.levelno >= logging.ERROR:
            level_color = ANSI_COLORS["RED"]
            emoji = LOG_EMOJI["ERROR"]
        elif record.levelno == logging.WARNING:
            level_color = ANSI_COLORS["YELLOW"]
            emoji = LOG_EMOJI["WARNING"]
        elif record.levelno == logging.INFO:
            level_color = ANSI_COLORS["CYAN"]
            emoji = LOG_EMOJI["INFO"]
        elif record.levelno == logging.DEBUG:
            level_color = ANSI_COLORS["BLUE"]
            emoji = LOG_EMOJI["DEBUG"]
        else:
            level_color = ANSI_COLORS["BRIGHT_BLACK"]
            emoji = "•"

        # Format message
        timestamp = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
        module = record.name.split(".")[-1]  # Last part of module name
        level_name = record.levelname.upper()

        # Build formatted string
        formatted = (
            f"{ANSI_COLORS['TIME']}{timestamp}{ANSI_COLORS['RESET']} "
            f"[{level_color}{level_name:8s}{ANSI_COLORS['RESET']}] "
            f"{emoji} "
            f"{ANSI_COLORS['BRIGHT_BLACK']}{module:20s}{ANSI_COLORS['RESET']} "
            f"→ {record.getMessage()}"
        )

        # Add extra fields if present
        if hasattr(record, "extra_fields") and record.extra_fields:
            extra_str = " | ".join(f"{k}={v}" for k, v in record.extra_fields.items())
            formatted += f"\n  {ANSI_COLORS['DIM']}{extra_str}{ANSI_COLORS['RESET']}"

        return formatted


# ══════════════════════════════════════════════════════════
# CUSTOM LOGGER CLASS
# ══════════════════════════════════════════════════════════

class KaraLogger(logging.Logger):
    """Extended logger with success() method and extra fields support."""

    def __init__(self, name: str, level: int = logging.NOTSET):
        super().__init__(name, level)
        self.has_rich = HAS_RICH

    def _log_with_extras(self, level: int, msg: str, **kwargs):
        """Internal method to log with extra fields."""
        record = self.makeRecord(
            self.name, level, "(unknown file)", 0,
            msg, (), None
        )
        # Attach extra fields to record
        record.extra_fields = {k: v for k, v in kwargs.items() if k not in ["exc_info"]}
        self.handle(record)

    def success(self, msg: str, **kwargs):
        """Log success level (green, with ✅)."""
        if self.isEnabledFor(logging.INFO):
            # Use INFO level but set custom level name
            record = self.makeRecord(
                self.name, logging.INFO, "(unknown file)", 0,
                msg, (), None
            )
            record.levelname = "SUCCESS"
            record.levelno = 25  # Between INFO(20) and WARNING(30)
            record.extra_fields = kwargs
            self.handle(record)

    def debug_obj(self, msg: str, obj: Any, **kwargs):
        """Log with object inspection."""
        import json
        try:
            obj_str = json.dumps(obj, indent=2, default=str)[:500]
        except:
            obj_str = str(obj)[:500]
        self.debug(f"{msg}\n{obj_str}", **kwargs)


# ══════════════════════════════════════════════════════════
# SETUP LOGGING
# ══════════════════════════════════════════════════════════

def setup_logging(
    log_level: str = "INFO",
    log_file: Optional[str] = None,
    use_rich: bool = True,
) -> None:
    """
    Configure global logging for KARA bot.

    Args:
        log_level: DEBUG, INFO, WARNING, ERROR, CRITICAL
        log_file: Optional file path for logging
        use_rich: Enable Rich colored output (disable for Railway if issues)
    """
    # Set custom logger class
    logging.setLoggerClass(KaraLogger)

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Create formatters based on environment
    if HAS_RICH and use_rich:
        # Rich console handler (beautiful colors)
        console = Console(
            theme=THEME,
            force_terminal=IS_LOCAL,  # Force colors locally
            force_unicode=True,
            width=120,
            record=True,
        )
        rich_handler = RichHandler(
            console=console,
            show_time=False,
            show_level=False,
            show_path=False,
            markup=False,
            rich_tracebacks=True,
            tracebacks_width=100,
        )
        rich_handler.setFormatter(
            logging.Formatter(
                fmt="%(message)s",
                datefmt="[%X]",
            )
        )
        root_logger.addHandler(rich_handler)

        # Also add custom formatter wrapper for better format
        class RichFormatterWrapper(logging.Formatter):
            """Wrapper to format logs nicely with Rich."""

            def format(self, record: logging.LogRecord) -> str:
                timestamp = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
                module = record.name.split(".")[-1]
                level_name = record.levelname.upper()

                # Map to emoji
                emoji = LOG_EMOJI.get(level_name, "•")

                # Build the log line with Rich markup
                msg = (
                    f"[dim]{timestamp}[/dim] "
                    f"[{THEME.get(level_name.lower(), 'white')} bold]"
                    f"[{level_name:8s}][/] "
                    f"{emoji} "
                    f"[bright_black]{module:20s}[/bright_black] "
                    f"→ {record.getMessage()}"
                )

                # Add extra fields
                if hasattr(record, "extra_fields") and record.extra_fields:
                    extra = " | ".join(f"{k}={v}" for k, v in record.extra_fields.items())
                    msg += f"\n  [dim]{extra}[/dim]"

                return msg

        # Replace handler formatter
        rich_handler.setFormatter(RichFormatterWrapper())

    else:
        # Fallback: ANSI colored handler (works everywhere)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(ColoredFormatter())
        root_logger.addHandler(console_handler)

    # Optional: file logging (always plain text)
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        file_handler = logging.FileHandler(log_path)
        file_handler.setFormatter(
            logging.Formatter(
                fmt="[%(asctime)s] [%(levelname)-8s] [%(name)s] → %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root_logger.addHandler(file_handler)


# ══════════════════════════════════════════════════════════
# GET LOGGER
# ══════════════════════════════════════════════════════════

def get_logger(name: str) -> KaraLogger:
    """
    Get a configured logger instance.

    Usage:
        from core.logger import get_logger
        log = get_logger(__name__)
        log.info("Hello world")
        log.success("Operation complete")
    """
    return logging.getLogger(name)


# ══════════════════════════════════════════════════════════
# INITIALIZE ON IMPORT
# ══════════════════════════════════════════════════════════

# Auto-setup logging on import
_ENV_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
_ENV_LOG_FILE = os.getenv("LOG_FILE", "kara.log")
_USE_RICH = os.getenv("DISABLE_RICH", "false").lower() != "true"

# Setup with environment variables
setup_logging(
    log_level=_ENV_LOG_LEVEL,
    log_file=_ENV_LOG_FILE if _ENV_LOG_FILE else None,
    use_rich=_USE_RICH,
)

# Announce logger ready
_root = logging.getLogger()
_root.debug(f"Logging system initialized - Level: {_ENV_LOG_LEVEL}, Rich: {_USE_RICH}")
