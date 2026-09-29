"""Build a venue adapter for scanner / paper / compare."""

from __future__ import annotations

from pathlib import Path

from polymarket_bot.config import AppConfig
from polymarket_bot.exchanges.kalshi import KalshiClient
from polymarket_bot.exchanges.polymarket import PolymarketClient
from polymarket_bot.market_data.fixture_client import FixtureClient
from polymarket_bot.market_data.replay_client import ReplayClient

_PM_VENUES = {"polymarket_us", "polymarket"}
PM_DISABLED = (
    "Polymarket US is disabled. Kalshi is the only active venue. "
    "Set polymarket_us_enabled: true in config.yaml to use the leftover adapter."
)
COMPARE_DISABLED = (
    "Cross-venue compare is disabled (Kalshi only). "
    "Set compare.enabled and polymarket_us_enabled in config.yaml to run it."
)


def default_fixture(exchange: str) -> Path:
    if exchange == "kalshi":
        return Path("fixtures/kalshi_markets.json")
    return Path("fixtures/public_markets.json")


def default_replay(exchange: str) -> Path:
    if exchange == "kalshi":
        return Path("fixtures/kalshi_replay.json")
    return Path("fixtures/replay.json")


def is_polymarket_venue(venue: str | None) -> bool:
    return (venue or "").lower() in _PM_VENUES


def assert_venue_enabled(config: AppConfig, venue: str | None) -> None:
    if is_polymarket_venue(venue) and not config.polymarket_us_enabled:
        raise SystemExit(PM_DISABLED)


def compare_is_enabled(config: AppConfig) -> bool:
    return bool(config.compare.enabled and config.polymarket_us_enabled)


def build_client(
    config: AppConfig,
    *,
    source: str,
    exchange: str | None = None,
    fixture: str | None = None,
):
    venue = (exchange or config.exchange).lower()
    assert_venue_enabled(config, venue)
    if source == "replay":
        path = Path(fixture or default_replay(venue))
        if path.exists():
            return ReplayClient(path)
        return ReplayClient(Path("fixtures/replay.json"))
    if source == "fixture":
        path = Path(fixture or default_fixture(venue))
        client = FixtureClient(path)
        client.venue = venue
        return client
    if source in {"live", "public"}:
        if venue == "kalshi":
            return KalshiClient(config)
        if is_polymarket_venue(venue):
            return PolymarketClient(config)
        raise SystemExit(f"unknown exchange {venue}")
    if source == "kalshi-demo-data":
        return KalshiClient(config, data_base_url=config.kalshi.demo_base_url)
    raise SystemExit(f"unknown source {source}")
