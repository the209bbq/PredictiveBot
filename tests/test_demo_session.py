from datetime import datetime, timezone
from decimal import Decimal

from polymarket_bot.account_risk import AccountRiskError
from polymarket_bot.config import TradingConfig, load_config
from polymarket_bot.demo.session import (
    assert_demo_order_within_risk,
    format_demo_report,
    run_demo_session,
)
from polymarket_bot.trading import TradingPaused, set_trading_enabled
from polymarket_bot.exchanges.kalshi import snapshot_from_kalshi
from polymarket_bot.guard import DemoOrderError
from polymarket_bot.logging_utils import DecisionLogger


class _DemoFake:
    venue = "kalshi"
    source_name = "fake-demo"
    demo_base_url = "https://demo-api.kalshi.co/trade-api/v2"
    data_base_url = "https://demo-api.kalshi.co/trade-api/v2"

    def __init__(self) -> None:
        self.placed: list[dict] = []
        self.cancelled = 0
        self.shutdowns = 0
        self._market = {
            "ticker": "KXDEMO-COIN",
            "slug": "KXDEMO-COIN",
            "title": "Demo coin",
            "status": "active",
            "yes_bid_dollars": "0.49",
            "yes_ask_dollars": "0.51",
            "close_time": "2027-01-01T00:00:00Z",
            "fee_type": "quadratic",
            "fee_multiplier": 1,
        }
        self._book = {
            "orderbook_fp": {
                "yes_dollars": [["0.4900", "80"]],
                "no_dollars": [["0.4900", "80"]],
            }
        }

    def _assert_demo(self, confirm_demo: bool) -> None:
        if not confirm_demo:
            raise DemoOrderError("Pass --confirm-demo")

    def _get(self, base, path, params=None):
        return {"market": self._market}

    def list_markets(self, *, limit, active=True, closed=False, offset=0, **_k):
        return [self._market]

    def book(self, slug: str):
        return self._book

    def last_trade(self, slug: str):
        return Decimal("0.50")

    def snapshot(self, market, book=None, *, now=None):
        return snapshot_from_kalshi(market, book, now=now)

    def place_demo_order(self, **kwargs):
        self.placed.append(kwargs)
        return {"order": {"order_id": f"o{len(self.placed)}"}}

    def cancel_all_demo_orders(self, *, confirm_demo):
        self.cancelled += 1
        return {}

    def shutdown_demo_orders(self, *, confirm_demo):
        self.shutdowns += 1
        return []

    def demo_balance(self, *, confirm_demo):
        return {"balance": "1000.00"}

    def demo_positions(self, *, confirm_demo):
        return {"market_positions": []}

    def demo_fills(self, *, confirm_demo, limit=100):
        return []

    def close(self):
        return None


def test_demo_session_quotes_requotes_and_verifies(tmp_path):
    cfg = load_config()
    client = _DemoFake()
    logger = DecisionLogger(tmp_path / "demo.jsonl")
    now = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)
    try:
        state = run_demo_session(
            client,
            cfg,
            logger,
            confirm_demo=True,
            ticks=2,
            ticker="KXDEMO-COIN",
            sleep=False,
            now=now,
        )
    finally:
        logger.close()
    assert state["demo"] is True
    assert state["live"] is False
    assert state["quotes_placed"] >= 2
    assert client.cancelled >= 2
    assert client.shutdowns == 1
    assert not state["resting_alert"]
    report = format_demo_report(state)
    assert "DEMO ONLY" in report
    assert "KXDEMO-COIN" in report
    assert "Quotes placed" in report
    assert "Resting leftover" in report


def test_demo_session_alerts_when_resting_remain(tmp_path):
    cfg = load_config()
    client = _DemoFake()

    def boom(*, confirm_demo):
        client.shutdowns += 1
        raise DemoOrderError("ALERT: resting Kalshi DEMO orders remain after cancel-all. Count=2")

    client.shutdown_demo_orders = boom  # type: ignore[method-assign]
    logger = DecisionLogger(tmp_path / "demo.jsonl")
    try:
        state = run_demo_session(
            client,
            cfg,
            logger,
            confirm_demo=True,
            ticks=1,
            ticker="KXDEMO-COIN",
            sleep=False,
        )
    finally:
        logger.close()
    assert state["resting_alert"]
    assert "ALERT" in state["resting_alert"]
    report = format_demo_report(state)
    assert "ALERT" in report
    text = (tmp_path / "demo.jsonl").read_text()
    assert "ALERT_RESTING_ORDERS" in text


def test_demo_session_requires_confirm_flag(tmp_path):
    cfg = load_config()
    client = _DemoFake()
    logger = DecisionLogger(tmp_path / "demo.jsonl")
    try:
        try:
            run_demo_session(client, cfg, logger, confirm_demo=False, ticks=1, ticker="X", sleep=False)
            raise AssertionError("should have raised")
        except DemoOrderError:
            pass
    finally:
        logger.close()


def test_demo_session_pauses_when_trading_off(tmp_path):
    cfg = load_config()
    object.__setattr__(
        cfg,
        "trading",
        TradingConfig(True, tmp_path / "toggle.json", tmp_path / "lock"),
    )
    set_trading_enabled(cfg.trading.toggle_path, False)
    client = _DemoFake()
    logger = DecisionLogger(tmp_path / "demo.jsonl")
    try:
        state = run_demo_session(
            client,
            cfg,
            logger,
            confirm_demo=True,
            ticks=2,
            ticker="KXDEMO-COIN",
            sleep=False,
        )
    finally:
        logger.close()
    assert state["quotes_placed"] == 0
    assert state["trading"] == "off"
    assert "trading_paused" in (tmp_path / "demo.jsonl").read_text()
    assert client.cancelled >= 1


def test_assert_demo_order_respects_toggle_and_cap(tmp_path):
    cfg = load_config()
    object.__setattr__(
        cfg,
        "trading",
        TradingConfig(True, tmp_path / "toggle.json", tmp_path / "lock"),
    )
    client = _DemoFake()
    client.list_demo_orders = lambda **k: []  # type: ignore[method-assign]
    set_trading_enabled(cfg.trading.toggle_path, False)
    try:
        assert_demo_order_within_risk(
            client, cfg, ticker="KXDEMO-COIN", side="bid", price="0.50", count="1", confirm_demo=True
        )
        raise AssertionError("should have raised")
    except TradingPaused:
        pass
    set_trading_enabled(cfg.trading.toggle_path, True)
    object.__setattr__(cfg.paper.risk, "max_account_risk_pct", Decimal("0.01"))
    try:
        assert_demo_order_within_risk(
            client,
            cfg,
            ticker="KXDEMO-COIN",
            side="bid",
            price="0.50",
            count="1000",
            confirm_demo=True,
        )
        raise AssertionError("should have raised")
    except AccountRiskError as exc:
        assert "exceed" in str(exc).lower()
