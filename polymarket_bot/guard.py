"""Hard dry-run guarantee: this version never talks to the live order API."""

from __future__ import annotations


class LiveTradingDisabled(RuntimeError):
    """Raised whenever a live-trading path is requested or accidentally invoked."""


_MESSAGE = (
    "Live trading is disabled in this version. The bot never places, cancels, "
    "or modifies real orders and never moves funds. Keep dry_run: true and "
    "live_trading_enabled: false. After KYC, keys from https://polymarket.us/developer "
    "would still require a future version that implements live order submission."
)


def assert_paper_only(*, dry_run: bool, live_trading_enabled: bool, env_live: str | None) -> None:
    env_on = str(env_live or "").strip().lower() in {"1", "true", "yes", "on"}
    if live_trading_enabled or env_on or not dry_run:
        raise LiveTradingDisabled(_MESSAGE)


def refuse_live_call(action: str) -> None:
    raise LiveTradingDisabled(f"Refusing live action {action!r}. {_MESSAGE}")
