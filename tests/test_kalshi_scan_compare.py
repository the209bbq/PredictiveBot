from datetime import datetime, timezone
from decimal import Decimal

from polymarket_bot.compare import compare_snapshots, title_score
from polymarket_bot.config import load_config
from polymarket_bot.exchanges.kalshi import snapshot_from_kalshi
from polymarket_bot.market_data.fixture_client import FixtureClient
from polymarket_bot.scanner import scan_markets


def test_kalshi_fixture_scanner():
    cfg = load_config()
    client = FixtureClient("fixtures/kalshi_markets.json")
    now = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)
    rows = scan_markets(client, cfg, now=now)
    assert client.venue == "kalshi"
    assert rows
    assert all(s.venue == "kalshi" for s in rows)
    assert all(s.best_bid is not None and s.best_ask is not None for s in rows)


def test_title_score_and_compare_synthetic():
    cfg = load_config()
    assert title_score("Philadelphia Eagles vs Chicago Bears", "Philadelphia vs Chicago") > 0.3
    now = datetime(2026, 9, 28, tzinfo=timezone.utc)
    k = snapshot_from_kalshi(
        {
            "ticker": "KXTEST-PHI",
            "title": "Philadelphia Eagles vs Chicago Bears",
            "event_title": "Philadelphia Eagles vs Chicago Bears",
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.42",
            "status": "active",
            "close_time": "2026-12-01T00:00:00Z",
            "fee_type": "quadratic",
            "fee_multiplier": 1,
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.4000", "50"]],
                "no_dollars": [["0.5800", "50"]],
            }
        },
        now=now,
    )
    p = snapshot_from_kalshi(
        {
            "ticker": "phi-chi",
            "title": "Philadelphia Eagles vs Chicago Bears",
            "event_title": "Philadelphia Eagles vs Chicago Bears",
            "yes_bid_dollars": "0.50",
            "yes_ask_dollars": "0.52",
            "status": "active",
            "close_time": "2026-12-01T00:00:00Z",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.5000", "50"]],
                "no_dollars": [["0.4800", "50"]],
            }
        },
        now=now,
    )
    p.venue = "polymarket_us"
    p.fee_type = "polymarket"
    p.fee_coefficient = Decimal("0.0695")
    gaps = compare_snapshots([k], [p], cfg)
    assert gaps
    assert gaps[0].buy_kalshi_sell_pm > 0
