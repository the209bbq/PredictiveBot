from datetime import datetime, timezone
from decimal import Decimal

from polymarket_bot.compare import compare_snapshots, format_compare_report, title_score
from polymarket_bot.config import load_config
from polymarket_bot.exchanges.kalshi import KalshiClient, snapshot_from_kalshi
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
    ws = title_score(
        "World Series Champion tec-mlb-champ-2026-09-27-atl",
        "Will Atlanta win the 2026 Pro Baseball Championship KXMLB-26-ATL",
    )
    assert ws > 0.34
    now = datetime(2026, 9, 28, tzinfo=timezone.utc)
    k = snapshot_from_kalshi(
        {
            "ticker": "KXMLB-26-ATL",
            "title": "Will Atlanta win the 2026 Pro Baseball Championship?",
            "event_title": "Will Atlanta win the 2026 Pro Baseball Championship?",
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
            "ticker": "tec-mlb-champ-2026-09-27-atl",
            "title": "World Series Champion",
            "event_title": "World Series Champion",
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
    assert gaps[0].per_contract_buy_k_sell_p == gaps[0].buy_kalshi_sell_pm / cfg.compare.contract_size


def test_hours_use_occurrence_datetime_not_late_close():
    now = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)
    snap = snapshot_from_kalshi(
        {
            "ticker": "KXNFLGAME-26SEP28PHICHI-CHI",
            "title": "Eagles vs Bears",
            "status": "active",
            "yes_bid_dollars": "0.49",
            "yes_ask_dollars": "0.51",
            "occurrence_datetime": "2026-09-29T02:00:00Z",
            "expected_expiration_time": "2026-09-29T06:00:00Z",
            "close_time": "2026-09-29T08:00:00Z",
        },
        None,
        now=now,
    )
    assert snap.hours_to_resolution is not None
    assert 3.5 < snap.hours_to_resolution < 4.5
    assert snap.hours_to_resolution < 6


def test_hours_use_expected_expiration_not_late_close():
    now = datetime(2026, 9, 28, tzinfo=timezone.utc)
    snap = snapshot_from_kalshi(
        {
            "ticker": "KXEVENT",
            "title": "Event today, official close next week",
            "status": "active",
            "yes_bid_dollars": "0.96",
            "yes_ask_dollars": "0.97",
            "close_time": "2026-10-05T00:00:00Z",
            "expected_expiration_time": "2026-09-29T00:00:00Z",
        },
        None,
        now=now,
    )
    assert snap.hours_to_resolution is not None
    assert 20 < snap.hours_to_resolution < 28


def test_min_net_edge_is_per_contract_not_clip_total():
    cfg = load_config()
    now = datetime(2026, 9, 28, tzinfo=timezone.utc)
    k = snapshot_from_kalshi(
        {
            "ticker": "KXMLB-26-ATL",
            "title": "Will Atlanta win the 2026 Pro Baseball Championship?",
            "event_title": "Will Atlanta win the 2026 Pro Baseball Championship?",
            "yes_bid_dollars": "0.10",
            "yes_ask_dollars": "0.10",
            "status": "active",
            "close_time": "2026-12-01T00:00:00Z",
            "fee_type": "quadratic",
            "fee_multiplier": 1,
        },
        {"orderbook_fp": {"yes_dollars": [["0.1000", "50"]], "no_dollars": [["0.9000", "50"]]}},
        now=now,
    )
    p = snapshot_from_kalshi(
        {
            "ticker": "tec-mlb-champ-2026-09-27-atl",
            "title": "World Series Champion",
            "event_title": "World Series Champion",
            "yes_bid_dollars": "0.12",
            "yes_ask_dollars": "0.13",
            "status": "active",
            "close_time": "2026-12-01T00:00:00Z",
        },
        {"orderbook_fp": {"yes_dollars": [["0.1200", "50"]], "no_dollars": [["0.8700", "50"]]}},
        now=now,
    )
    p.venue = "polymarket_us"
    p.fee_type = "polymarket"
    p.fee_coefficient = Decimal("0.0695")
    gaps = compare_snapshots([k], [p], cfg)
    assert gaps
    gap = gaps[0]
    assert gap.buy_kalshi_sell_pm >= Decimal("0.01")
    assert gap.per_contract_buy_k_sell_p < cfg.compare.min_net_edge
    report = format_compare_report(gaps, cfg)
    assert "PER CONTRACT" in report
    assert "ALERT" not in report.split("matched markets")[0]


def test_list_markets_paginates_with_cursor():
    cfg = load_config()
    client = KalshiClient(cfg)
    pages = [
        {"markets": [{"ticker": f"A{i}", "status": "open"} for i in range(200)], "cursor": "c1"},
        {"markets": [{"ticker": f"B{i}", "status": "open"} for i in range(200)], "cursor": "c2"},
        {"markets": [{"ticker": f"C{i}", "status": "open"} for i in range(50)], "cursor": None},
    ]
    calls: list[dict] = []

    def fake_get(_base, path, params=None):
        assert path == "/markets"
        calls.append(dict(params or {}))
        return pages[len(calls) - 1]

    client._get = fake_get  # type: ignore[method-assign]
    try:
        rows = client.list_markets(limit=450, active=True, closed=False)
    finally:
        client.close()
    assert len(rows) == 450
    assert len(calls) == 3
    assert calls[1]["cursor"] == "c1"
    assert calls[2]["cursor"] == "c2"
    assert rows[0]["slug"] == "A0"
    assert rows[-1]["slug"] == "C49"


def test_last_trade_reads_yes_price_dollars():
    cfg = load_config()
    client = KalshiClient(cfg)

    def fake_get(_base, path, params=None):
        assert path == "/markets/trades"
        assert params["ticker"] == "KXDEMO"
        return {"trades": [{"yes_price_dollars": "0.4700"}]}

    client._get = fake_get  # type: ignore[method-assign]
    try:
        assert client.last_trade("KXDEMO") == Decimal("0.4700")
    finally:
        client.close()


def test_trades_pages_min_ts_and_returns_oldest_first():
    cfg = load_config()
    client = KalshiClient(cfg)
    calls = []

    def fake_get(_base, path, params=None):
        calls.append(params)
        if not params.get("cursor"):
            return {
                "trades": [
                    {
                        "yes_price_dollars": "0.40",
                        "count": "2",
                        "created_time": "2026-09-29T12:01:00Z",
                        "trade_id": "b",
                    }
                ],
                "cursor": "c2",
            }
        return {
            "trades": [
                {
                    "yes_price_dollars": "0.39",
                    "count": "1",
                    "created_time": "2026-09-29T12:00:00Z",
                    "trade_id": "a",
                }
            ]
        }

    client._get = fake_get  # type: ignore[method-assign]
    try:
        tape = client.trades("KXRT-FOO", min_ts=1000)
    finally:
        client.close()
    assert calls[0]["min_ts"] == 1000
    assert [t.trade_id for t in tape] == ["a", "b"]
    assert tape[0].qty == Decimal("1")
