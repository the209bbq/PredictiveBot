"""Trading on/off toggle and single-process lock.

The config flag is the master switch. `pmbot trading off` writes a small state
file so a running loop pauses without a code or config edit. Checked every tick.
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.config import AppConfig


class TradingPaused(RuntimeError):
    """Raised when a one-shot order is refused because trading is off."""


class TradingLockHeld(RuntimeError):
    """Raised when another trading process already holds the lock."""


def set_trading_enabled(path: Path, enabled: bool) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "enabled": bool(enabled),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return payload


def read_toggle(path: Path) -> bool | None:
    """Return the file flag, or None if the file is absent (treat as on)."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError:
        return None
    if "enabled" not in data:
        return None
    return bool(data.get("enabled"))


def trading_is_on(config: AppConfig) -> tuple[bool, str]:
    """AND of config.trading.enabled and the toggle file. Re-read every call."""
    if not config.trading.enabled:
        return False, "config_disabled"
    flag = read_toggle(config.trading.toggle_path)
    if flag is False:
        return False, "toggle_off"
    return True, "on"


def trading_status(config: AppConfig) -> dict[str, Any]:
    on, reason = trading_is_on(config)
    file_flag = read_toggle(config.trading.toggle_path)
    return {
        "effective": "on" if on else "off",
        "reason": reason,
        "config_enabled": config.trading.enabled,
        "toggle_path": str(config.trading.toggle_path),
        "toggle_file": None if file_flag is None else ("on" if file_flag else "off"),
        "lock_path": str(config.trading.lock_path),
    }


class TradingLock:
    """Exclusive flock + PID file. A second trading instance refuses to start."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            info = handle.read().strip() or "unknown holder"
            handle.close()
            raise TradingLockHeld(
                f"Another trading process is already running ({info}). "
                f"Lock: {self.path}"
            ) from None
        handle.seek(0)
        handle.truncate()
        handle.write(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "acquired_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            + "\n"
        )
        handle.flush()
        self._fd = handle

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
        finally:
            self._fd.close()
            self._fd = None

    def __enter__(self) -> "TradingLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
