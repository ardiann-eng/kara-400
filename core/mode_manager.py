"""
KARA Bot - Mode Manager
Central singleton controlling Standard vs Scalper trading mode.
Switch at runtime via Telegram or Dashboard — no restart required.

Standard Mode: Swing/positional trades, hold hours-to-days, safe for students.
Scalper Mode : Ultra-aggressive HFT, 10-40 trades/day, max 12min hold.
               ⚠️  35x leverage, 13% risk per trade — EXTREME RISK.
"""

from __future__ import annotations
import logging
import time
from typing import Optional, Callable, List

log = logging.getLogger("kara.mode_manager")


class ModeManager:
    """
    Singleton that tracks and switches the active trading mode.
    Injected into: RiskManager, ScoringEngine, KaraTelegram, main.py

    Design rules:
    - Existing open positions keep their original mode's exit rules.
    - New positions always use the CURRENT mode's parameters.
    - Pyramid in scalper mode requires explicit Telegram confirmation.
    """

    STANDARD = "standard"
    SCALPER  = "scalper"
    VALID_MODES = {STANDARD, SCALPER}

    def __init__(self, initial_mode: str = "scalper"):
        import config
        # Determine initial mode from env/config or argument
        if getattr(config, "FORCE_SCALPER_ONLY", False):
            mode = self.SCALPER
        else:
            mode = initial_mode.lower() if initial_mode else config.TRADING_MODE
        self._mode: str = mode if mode in self.VALID_MODES else self.SCALPER
        self._switched_at: float = time.monotonic()
        # Callbacks to notify interested parties (e.g. main.py loop) on switch
        self._on_switch_callbacks: List[Callable[[str], None]] = []

        force = getattr(config, "FORCE_SCALPER_ONLY", False)
        log.info(
            f"🎛️  ModeManager initialized — mode: {self._mode.upper()}"
            f"{' (FORCE_SCALPER_ONLY)' if force else ''}"
        )

    # ──────────────────────────────────────────
    # GETTERS
    # ──────────────────────────────────────────

    @property
    def mode(self) -> str:
        """Current active trading mode string."""
        return self._mode

    def is_scalper(self) -> bool:
        """True when Scalper Mode is active."""
        import config
        if getattr(config, "FORCE_SCALPER_ONLY", False):
            return True
        return self._mode == self.SCALPER

    def is_standard(self) -> bool:
        """True when Standard Mode is active."""
        import config
        if getattr(config, "FORCE_SCALPER_ONLY", False):
            return False
        return self._mode == self.STANDARD

    def get_config(self):
        """
        Return the active mode's config object.
        Returns SCALPER config if scalper, else RISK config.
        """
        import config
        return config.SCALPER if self.is_scalper() else config.RISK

    @property
    def scan_interval(self) -> int:
        """Scan interval seconds for the current mode."""
        import config
        return config.SCALPER.scan_interval_seconds if self.is_scalper() else 60

    @property
    def min_score(self) -> int:
        """Minimum score threshold for the current mode."""
        import config
        if self.is_scalper():
            return config.SCALPER.min_score_to_enter
        return config.SIGNAL.min_score_to_signal

    @property
    def signal_cooldown_minutes(self) -> int:
        """Signal cooldown for the current mode."""
        import config
        return config.SCALPER.signal_cooldown_minutes if self.is_scalper() else config.SIGNAL.signal_cooldown_minutes

    # ──────────────────────────────────────────
    # SWITCHING
    # ──────────────────────────────────────────

    def switch(self, new_mode: str) -> bool:
        """
        Switch to new_mode. Returns True if mode actually changed.
        Existing positions are NOT force-closed — they run out under old rules.
        """
        import config
        new_mode = new_mode.lower()

        # Hard lock: standard cannot be selected while FORCE_SCALPER_ONLY
        if getattr(config, "FORCE_SCALPER_ONLY", False) and new_mode != self.SCALPER:
            log.warning(
                f"Mode switch to {new_mode.upper()} blocked — FORCE_SCALPER_ONLY is on"
            )
            if self._mode != self.SCALPER:
                old_mode = self._mode
                self._mode = self.SCALPER
                self._switched_at = time.monotonic()
                log.warning(f"🔀 Trading mode forced: {old_mode.upper()} → SCALPER")
                for cb in self._on_switch_callbacks:
                    try:
                        cb(self.SCALPER)
                    except Exception as e:
                        log.error(f"Mode switch callback error: {e}")
                return True
            return False

        if new_mode not in self.VALID_MODES:
            log.error(f"Invalid mode: {new_mode}. Must be 'standard' or 'scalper'.")
            return False
        if new_mode == self._mode:
            log.info(f"Mode already {new_mode.upper()} — no change.")
            return False

        old_mode = self._mode
        self._mode = new_mode
        self._switched_at = time.monotonic()
        log.warning(f"🔀 Trading mode switched: {old_mode.upper()} → {new_mode.upper()}")

        # Notify listeners (e.g. main.py updates scan interval)
        for cb in self._on_switch_callbacks:
            try:
                cb(new_mode)
            except Exception as e:
                log.error(f"Mode switch callback error: {e}")

        return True

    def register_on_switch(self, callback: Callable[[str], None]):
        """Register a callback to be called whenever the mode changes."""
        self._on_switch_callbacks.append(callback)

    # ──────────────────────────────────────────
    # STATUS
    # ──────────────────────────────────────────

    @property
    def status(self) -> dict:
        """Status dict for Dashboard/Telegram display."""
        mins_since_switch = int((time.monotonic() - self._switched_at) / 60)
        return {
            "mode":              self._mode,
            "is_scalper":        self.is_scalper(),
            "mins_since_switch": mins_since_switch,
            "scan_interval_s":   self.scan_interval,
            "min_score":         self.min_score,
        }


# ── Module-level singleton ─────────────────────────────────────────────────
# Import this from anywhere: `from core.mode_manager import mode_manager`
mode_manager = ModeManager()
