from datetime import datetime, timedelta, timezone
from decimal import Decimal

from polymarket_bot.config import load_config
from polymarket_bot.demo.session import (
    LiveQuote,
    merge_watched_markets,
    quote_target_unchanged,
    run_demo_session,
)
from polymarket_bot.exchanges.kalshi import snapshot_from_kalshi
from polymarket_bot.logging_utils import DecisionLogger
from polymarket_bot.paper.portfolio import PaperOrder
from polymarket_bot.scanner import last_scan_coverage, last_scan_summary, scan_maker_universe


NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def _fav_cfg():
    cfg = load_config(environment="demo")
    paper = dict(cfg.extra.get("paper") or {})
    fav = dict(paper.get("favorites") or {})
    fav["rescan_minutes"] = 0
    fav["max_markets"] = 5
    paper["favorites"] = fav
    paper["strategy"] = "favorites_maker"
    cfg.extra["paper"] = paper
    return cfg


def _weather_row(ticker: str, close: str = "2026-09-29T15:00:00Z") -> dict:
    return {
        "ticker": ticker,
        "slug": ticker,
        "series_ticker": "KXRAIN",
        "title": "Rain",
        "category": "Climate and Weather",
        "status": "open",
        "yes_bid_dollars": "0.1400",
        "yes_ask_dollars": "0.1600",
        "close_time": close,
        "fee_type": "quadratic",
        "fee_multiplier": 1,
        "volume_fp": "20",
    }


class _FavDemoFake:
    venue = "kalshi"
    source_name = "fake-demo"
    demo_base_url = "https://demo-api.kalshi.co/trade-api/v2"
    data_base_url = "https://demo-api.kalshi.co/trade-api/v2"

    def __init__(self, markets: list[dict] | None = None) -> None:
        self.markets = list(markets or [_weather_row("KXRAIN-29SEP26-DEN")])
        self.placed: list[dict] = []
        self.cancelled_bot = 0
        self.cancelled_all = 0
        self.cancelled_one: list[dict] = []
        self.shutdowns = 0
        self.shutdown_emergency: bool | None = None
        self.list_series_calls = 0
        self.list_markets_series: list[str | None] = []
        self._positions: dict = {"market_positions": []}
        self._book = {
            "orderbook_fp": {
                "yes_dollars": [["0.1400", "12"]],
                "no_dollars": [["0.8400", "12"]],
            }
        }
        self.fail_series: set[str] = set()

    def _assert_demo(self, confirm_demo: bool) -> None:
        if not confirm_demo:
            raise AssertionError("confirm-demo required")

    def _get(self, base, path, params=None):
        ticker = str(path).rsplit("/", 1)[-1]
        for row in self.markets:
            if row["ticker"] == ticker:
                return {"market": row}
        return {"market": self.markets[0]}

    def list_series(self, **_k):
        self.list_series_calls += 1
        return [{"ticker": "KXRAIN"}, {"ticker": "KXHIGH"}]

    def list_markets(self, *, limit, active=True, closed=False, offset=0, series_ticker=None, **_k):
        self.list_markets_series.append(series_ticker)
        if series_ticker in self.fail_series:
            raise RuntimeError("HTTP 429")
        if series_ticker:
            return [row for row in self.markets if str(row.get("series_ticker")) == series_ticker]
        return list(self.markets)

    def book(self, slug: str):
        return self._book

    def last_trade(self, slug: str):
        return Decimal("0.15")

    def get_market(self, ticker: str):
        for row in self.markets:
            if row["ticker"] == ticker:
                return {"market": row}
        return {"market": {"ticker": ticker, "status": "open"}}

    def snapshot(self, market, book=None, *, now=None):
        return snapshot_from_kalshi(market, book, now=now)

    def place_demo_order(self, **kwargs):
        self.placed.append(kwargs)
        return {"order": {"order_id": f"o{len(self.placed)}"}}

    def cancel_all_demo_orders(self, *, confirm_demo):
        self.cancelled_all += 1
        return {}

    def cancel_bot_demo_orders(self, *, confirm_demo):
        self.cancelled_bot += 1
        return {"cancelled": 1}

    def cancel_demo_order(self, order_id, *, ticker=None, confirm_demo=False):
        self.cancelled_one.append({"order_id": order_id, "ticker": ticker})
        return {}

    def list_demo_orders(self, *, status="resting", confirm_demo=False):
        return []

    def shutdown_demo_orders(self, *, confirm_demo, emergency_all=False):
        self.shutdowns += 1
        self.shutdown_emergency = emergency_all
        return []

    def demo_balance(self, *, confirm_demo):
        return {"balance": 100000, "balance_dollars": "1000.00", "portfolio_value": 0}

    def demo_positions(self, *, confirm_demo):
        return self._positions

    def demo_fills(self, *, confirm_demo, limit=100):
        return []

    def close(self):
        return None


def test_quote_target_unchanged_same_price_size():
    order = PaperOrder(
        order_id="x",
        market="KXRAIN-A",
        side="buy",
        price=Decimal("0.85"),
        qty=Decimal("2"),
        strategy="favorites_maker",
        contract_side="no",
    )
    live = LiveQuote(
        ticker="KXRAIN-A",
        order=order,
        book_side="ask",
        yes_price=Decimal("0.1500"),
        qty=Decimal("2"),
        order_id="o1",
    )
    assert quote_target_unchanged(live, order, "ask", Decimal("0.1500")) is True
    changed = PaperOrder(
        order_id="y",
        market="KXRAIN-A",
        side="buy",
        price=Decimal("0.86"),
        qty=Decimal("2"),
        strategy="favorites_maker",
        contract_side="no",
    )
    assert quote_target_unchanged(live, changed, "ask", Decimal("0.1400")) is False


def test_merge_watched_keeps_current_and_fills_cap():
    from polymarket_bot.exchanges.kalshi import snapshot_from_kalshi

    def snap(ticker):
        row = _weather_row(ticker)
        out = snapshot_from_kalshi(row, None, now=NOW)
        out.slug = ticker
        return out

    current = {"KXRAIN-A": snap("KXRAIN-A")}
    scanned = [snap("KXRAIN-B"), snap("KXRAIN-C"), snap("KXRAIN-A")]
    merged = merge_watched_markets(current, scanned, max_markets=2, pinned="KXRAIN-A")
    assert list(merged) == ["KXRAIN-A", "KXRAIN-B"]


def test_favorites_demo_quotes_all_kept_markets_and_does_not_requote(tmp_path):
    cfg = _fav_cfg()
    client = _FavDemoFake(
        [
            _weather_row("KXRAIN-29SEP26-DEN"),
            _weather_row("KXRAIN-29SEP26-NY"),
            _weather_row("KXRAIN-29SEP26-CHI"),
        ]
    )
    logger = DecisionLogger(tmp_path / "demo.jsonl")
    try:
        state = run_demo_session(
            client,
            cfg,
            logger,
            confirm_demo=True,
            ticks=2,
            ticker=None,
            sleep=False,
            now=NOW,
        )
    finally:
        logger.close()
    markets = set(state["markets"])
    assert markets == {"KXRAIN-29SEP26-DEN", "KXRAIN-29SEP26-NY", "KXRAIN-29SEP26-CHI"}
    tickers = {row["ticker"] for row in client.placed}
    assert tickers == markets
    first_count = len({row["ticker"] for row in client.placed})
    assert state["quotes_placed"] == first_count
    assert len(client.placed) == first_count
    assert client.cancelled_one == []
    assert state["strategy"] == "favorites_maker"
    live = cfg.logging.demo_state_path
    assert live.exists()
    import json

    payload = json.loads(live.read_text())
    assert len(payload.get("resting_orders") or []) == first_count
    pointer = tmp_path / "pmbot-state" / "current"
    assert pointer.exists()
    assert "demo_state" in pointer.read_text()


def test_favorites_demo_drops_market_past_close(tmp_path):
    cfg = _fav_cfg()
    closing = _weather_row("KXRAIN-29SEP26-DEN", close="2026-09-29T15:00:00Z")
    stay = _weather_row("KXRAIN-29SEP26-NY", close="2026-09-29T16:00:00Z")
    client = _FavDemoFake([closing, stay])

    def snapshot(market, book=None, *, now=None):
        row = dict(market)
        if row.get("ticker") == "KXRAIN-29SEP26-DEN" and client.placed:
            row["close_time"] = "2026-09-29T11:00:00Z"
            row["status"] = "closed"
        return snapshot_from_kalshi(row, book, now=now)

    client.snapshot = snapshot  # type: ignore[method-assign]
    logger = DecisionLogger(tmp_path / "demo.jsonl")
    try:
        state = run_demo_session(
            client,
            cfg,
            logger,
            confirm_demo=True,
            ticks=2,
            ticker=None,
            sleep=False,
            now=NOW,
        )
    finally:
        logger.close()
    assert "KXRAIN-29SEP26-DEN" not in state["markets"]
    assert "KXRAIN-29SEP26-NY" in state["markets"]
    assert any(row["ticker"] == "KXRAIN-29SEP26-DEN" for row in client.cancelled_one)


def test_favorites_book_wide_capital_cap(tmp_path):
    cfg = _fav_cfg()
    object.__setattr__(cfg.paper.risk, "max_daily_capital_in_use_usd", Decimal("3"))
    client = _FavDemoFake(
        [
            _weather_row("KXRAIN-29SEP26-DEN"),
            _weather_row("KXRAIN-29SEP26-NY"),
            _weather_row("KXRAIN-29SEP26-CHI"),
        ]
    )
    logger = DecisionLogger(tmp_path / "demo.jsonl")
    try:
        state = run_demo_session(
            client,
            cfg,
            logger,
            confirm_demo=True,
            ticks=1,
            ticker=None,
            sleep=False,
            now=NOW,
        )
    finally:
        logger.close()
    assert state["quotes_placed"] == 1
    assert len({row["ticker"] for row in client.placed}) == 1


def test_series_catalog_cached_and_coverage_logs_failures():
    cfg = _fav_cfg()
    client = _FavDemoFake([_weather_row("KXRAIN-29SEP26-DEN")])
    client.fail_series.add("KXHIGH")
    rows = scan_maker_universe(client, cfg, now=NOW)
    assert [s.slug for s in rows] == ["KXRAIN-29SEP26-DEN"]
    first_calls = client.list_series_calls
    assert first_calls == 1
    scan_maker_universe(client, cfg, now=NOW + timedelta(minutes=1))
    assert client.list_series_calls == first_calls
    coverage = last_scan_coverage()
    assert coverage["series_attempted"] >= 2
    assert coverage["series_failed"] >= 1
    assert "KXHIGH" in coverage["series_failed_ids"]
    summary = last_scan_summary()
    assert "series_attempted=" in summary
    assert "series_failed=" in summary
