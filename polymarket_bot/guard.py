"""Safety rails: production trading is never enabled. Kalshi demo orders are opt-in."""

from __future__ import annotations

from urllib.parse import urlparse


class LiveTradingDisabled(RuntimeError):
    """Raised whenever a production/live-trading path is requested."""


class DemoOrderError(RuntimeError):
    """Raised when Kalshi demo-order mode is misconfigured or blocked."""


_LIVE_MESSAGE = (
    "Production trading is disabled. This bot never places, cancels, or modifies "
    "real orders on Polymarket US or Kalshi production. Keep dry_run: true and "
    "live_trading_enabled: false. Kalshi DEMO orders are a separate opt-in path "
    "that only talks to https://demo-api.kalshi.co/trade-api/v2."
)

KALSHI_PRODUCTION_HOSTS = {
    "external-api.kalshi.com",
    "api.elections.kalshi.com",
    "trading-api.kalshi.com",
    "kalshi.com",
    "www.kalshi.com",
}
KALSHI_DEMO_HOSTS = {
    "external-api.demo.kalshi.co",
    "demo-api.kalshi.co",
    "demo.kalshi.co",
}


def _host(url: str) -> str:
    return (urlparse(url).hostname or url).lower().strip()


def is_kalshi_production_url(url: str) -> bool:
    host = _host(url)
    if "demo" in host:
        return False
    return host in KALSHI_PRODUCTION_HOSTS or host.endswith(".kalshi.com")


def is_kalshi_demo_url(url: str) -> bool:
    host = _host(url)
    return host in KALSHI_DEMO_HOSTS or "demo.kalshi." in host


def assert_paper_only(*, dry_run: bool, live_trading_enabled: bool, env_flags: list[str | None]) -> None:
    extra_on = any(str(flag or "").strip().lower() in {"1", "true", "yes", "on"} for flag in env_flags)
    if live_trading_enabled or extra_on or not dry_run:
        raise LiveTradingDisabled(_LIVE_MESSAGE)


def refuse_live_call(action: str) -> None:
    raise LiveTradingDisabled(f"Refusing live action {action!r}. {_LIVE_MESSAGE}")


def assert_kalshi_demo_orders_allowed(*, enabled: bool, base_url: str) -> None:
    if not enabled:
        raise DemoOrderError(
            "Kalshi demo orders are off. Set kalshi.demo_orders_enabled: true and "
            "pass --confirm-demo. Production trading remains disabled."
        )
    if is_kalshi_production_url(base_url):
        raise DemoOrderError(
            f"Refusing to send orders: {base_url} looks like Kalshi production. "
            "Demo orders must use https://demo-api.kalshi.co/trade-api/v2 "
            "(or the documented fallback https://external-api.demo.kalshi.co/trade-api/v2)."
        )
    if not is_kalshi_demo_url(base_url):
        raise DemoOrderError(
            f"Refusing to send orders: {base_url} is not a known Kalshi demo host."
        )
