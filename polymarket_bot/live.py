"""Live-trading stub. This version always raises."""

from __future__ import annotations

from polymarket_bot.guard import refuse_live_call


def start_live_trading() -> None:
    refuse_live_call("start_live_trading")
