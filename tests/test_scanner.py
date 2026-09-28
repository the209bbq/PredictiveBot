from datetime import datetime, timezone
from decimal import Decimal

from polymarket_bot.config import load_config
from polymarket_bot.market_data.fixture_client import FixtureClient
from polymarket_bot.scanner import is_liquid, scan_markets


def test_scan_fixtures_returns_liquid_rows():
    cfg = load_config()
    client = FixtureClient("fixtures/public_markets.json")
    now = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)
    rows = scan_markets(client, cfg, now=now)
    assert rows
    assert all(is_liquid(s, cfg.scanner) for s in rows)
    assert all(s.spread is not None and s.spread <= cfg.scanner.max_spread for s in rows)
    assert all(s.volume_shares and s.volume_shares >= cfg.scanner.min_volume_shares for s in rows)


def test_illiquid_wide_spread_filtered():
    cfg = load_config()
    client = FixtureClient("fixtures/public_markets.json")
    now = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)
    rows = scan_markets(client, cfg, now=now)
    snap = rows[0]
    snap.spread = Decimal("0.50")
    snap.best_ask = snap.best_bid + snap.spread if snap.best_bid is not None else Decimal("0.90")
    assert not is_liquid(snap, cfg.scanner)
