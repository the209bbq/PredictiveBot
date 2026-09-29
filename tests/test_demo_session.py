from datetime import datetime, timezone
from decimal import Decimal

from polymarket_bot.account_risk import AccountRiskError
from polymarket_bot.config import TradingConfig, load_config
from polymarket_bot.demo.session import (
    assert_demo_order_within_risk,
    format_demo_report,
    run_demo_session,
)
from polymarket_bot.daily_limits import refresh_daily_limits
from polymarket_bot.paper.portfolio import Portfolio
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
        self.cancelled_all = 0
        self.cancelled_bot = 0
        self.cancelled_one: list[dict] = []
        self.shutdowns = 0
        self.shutdown_emergency: bool | None = None
        self._positions: dict = {"market_positions": []}
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
        self.cancelled_all += 1
        return {}

    def cancel_bot_demo_orders(self, *, confirm_demo):
        self.cancelled += 1
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
    # Same book and <30s: second tick does not cancel/replace.
    assert client.cancelled_bot >= 1
    assert client.cancelled_all == 0
    assert client.shutdowns == 1
    assert client.shutdown_emergency is False
    assert not state["resting_alert"]
    report = format_demo_report(state)
    assert "DEMO ONLY" in report
    assert "KXDEMO-COIN" in report
    assert "Quotes placed" in report
    assert "Resting leftover" in report
    assert "Market risk" in report
    assert state["cancels_scoped"] is True


def test_demo_session_alerts_when_resting_remain(tmp_path):
    cfg = load_config()
    client = _DemoFake()

    def boom(*, confirm_demo, emergency_all=False):
        client.shutdowns += 1
        client.shutdown_emergency = emergency_all
        raise DemoOrderError("ALERT: resting Kalshi DEMO orders this bot placed remain after cancel. Count=2")

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
    assert client.cancelled_bot >= 1
    assert client.cancelled_all == 0


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


def test_leftover_positions_count_in_account_risk(tmp_path):
    cfg = load_config()
    client = _DemoFake()
    client._positions = {
        "market_positions": [
            {"ticker": "OLD-LEFTOVER", "position_fp": "200.00", "market_exposure_dollars": "100.00"},
        ]
    }
    logger = DecisionLogger(tmp_path / "demo.jsonl")
    now = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)
    try:
        state = run_demo_session(
            client,
            cfg,
            logger,
            confirm_demo=True,
            ticks=1,
            ticker="KXDEMO-COIN",
            sleep=False,
            now=now,
        )
    finally:
        logger.close()
    risk = state["account_risk"]
    # leftover 200 * 0.50 = $100 on $1000 = 10%, plus any new quotes
    assert Decimal(str(risk["at_risk"])) >= Decimal("100")
    assert "OLD-LEFTOVER" in ((state.get("maker") or {}).get("positions") or {})
    report = format_demo_report(state)
    assert "OLD-LEFTOVER" in report or "100" in str(risk["at_risk"])


def test_leftover_positions_can_block_new_quotes(tmp_path):
    cfg = load_config()
    object.__setattr__(cfg.paper.risk, "max_account_risk_pct", Decimal("0.05"))
    client = _DemoFake()
    client._positions = {
        "market_positions": [
            {"ticker": "OLD-LEFTOVER", "position_fp": "200.00", "market_exposure_dollars": "100.00"},
        ]
    }
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
    assert state["quotes_placed"] == 0
    assert "account_risk_cap" in (tmp_path / "demo.jsonl").read_text() or Decimal(
        str(state["account_risk"]["pct"])
    ) > Decimal("0.05")


def test_demo_skips_live_nfl_game_inside_maker_hours_window(tmp_path):
    cfg = load_config()
    client = _DemoFake()
    now = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)
    client._market = {
        "ticker": "KXNFLGAME-26SEP28PHICHI-CHI",
        "slug": "KXNFLGAME-26SEP28PHICHI-CHI",
        "title": "Eagles vs Bears",
        "event_title": "PHI vs CHI",
        "category": "Sports",
        "status": "active",
        "yes_bid_dollars": "0.49",
        "yes_ask_dollars": "0.51",
        "occurrence_datetime": "2026-09-28T20:15:00Z",
        "expected_expiration_time": "2026-09-29T02:00:00Z",
        "close_time": "2026-09-29T06:00:00Z",
        "fee_type": "quadratic",
        "fee_multiplier": 1,
    }
    logger = DecisionLogger(tmp_path / "demo.jsonl")
    try:
        state = run_demo_session(
            client,
            cfg,
            logger,
            confirm_demo=True,
            ticks=1,
            ticker="KXNFLGAME-26SEP28PHICHI-CHI",
            sleep=False,
            now=now,
        )
    finally:
        logger.close()
    assert state["quotes_placed"] == 0
    text = (tmp_path / "demo.jsonl").read_text()
    assert (
        "live_in_game" in text
        or "market_risk_score" in text
        or "resolution_cutoff" in text
        or "series_filter" in text
    )
    score = (state.get("market_risk") or {}).get("KXNFLGAME-26SEP28PHICHI-CHI") or {}
    assert Decimal(str(score.get("score") or 0)) >= Decimal("0.95")
    report = format_demo_report(state)
    assert "live-game" in report or "95" in report or state["quotes_placed"] == 0


def test_demo_scan_skips_denied_sports(tmp_path):
    cfg = load_config()
    client = _DemoFake()
    client._markets = [
        {
            "ticker": "KXNFLGAME-26SEP28PHICHI-CHI",
            "slug": "KXNFLGAME-26SEP28PHICHI-CHI",
            "series_ticker": "KXNFLGAME",
            "title": "Eagles vs Bears",
            "status": "active",
            "yes_bid_dollars": "0.49",
            "yes_ask_dollars": "0.51",
            "close_time": "2027-01-01T00:00:00Z",
            "fee_type": "quadratic",
            "volume_fp": "99999",
            "yes_bid_size_fp": "500",
            "yes_ask_size_fp": "500",
        },
        {
            "ticker": "KXATPMATCH-X",
            "slug": "KXATPMATCH-X",
            "series_ticker": "KXATPMATCH",
            "title": "Tennis match",
            "status": "active",
            "yes_bid_dollars": "0.49",
            "yes_ask_dollars": "0.51",
            "close_time": "2027-01-01T00:00:00Z",
            "fee_type": "quadratic",
            "volume_fp": "88888",
            "yes_bid_size_fp": "500",
            "yes_ask_size_fp": "500",
        },
        dict(client._market),
    ]

    def list_markets(*, limit, active=True, closed=False, offset=0, **_k):
        return list(client._markets)

    client.list_markets = list_markets  # type: ignore[method-assign]
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
        )
    finally:
        logger.close()
    assert state["markets"] == ["KXDEMO-COIN"]
    assert (tmp_path / "pmbot-state" / "demo_state.json").exists() or cfg.logging.demo_state_path.exists()


def test_unrealized_loss_trips_demo_daily_stop(tmp_path):
    cfg = load_config()
    object.__setattr__(
        cfg,
        "trading",
        TradingConfig(True, tmp_path / "toggle.json", tmp_path / "lock"),
    )
    now = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)
    # Start of PT day: cash 900 + 200 marked at 0.50 = $1000. Cash does not move.
    refresh_daily_limits(cfg, equity=Decimal("1000"), capital_in_use=Decimal("100"), now=now)
    port = Portfolio("kalshi_demo", Decimal("900"), Decimal("1000"))
    port.position("KXRT-FOO").qty = Decimal("200")
    port.position("KXRT-FOO").avg_price = Decimal("0.50")
    assert port.equity({"KXRT-FOO": Decimal("0.50")}) == Decimal("1000")
    equity = port.equity({"KXRT-FOO": Decimal("0.20")})
    assert equity == Decimal("940")
    hit = refresh_daily_limits(cfg, equity=equity, capital_in_use=Decimal("40"), now=now)
    assert hit.loss_halted is True
    assert hit.day_pnl == Decimal("-60")


def test_demo_session_unrealized_mark_stops_new_quotes(tmp_path):
    cfg = load_config()
    object.__setattr__(
        cfg,
        "trading",
        TradingConfig(True, tmp_path / "toggle.json", tmp_path / "lock"),
    )
    now = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)
    refresh_daily_limits(cfg, equity=Decimal("1000"), capital_in_use=Decimal("100"), now=now)
    client = _DemoFake()
    client.demo_balance = lambda **k: {  # type: ignore[method-assign]
        "balance": 90000,
        "balance_dollars": "900.00",
        "portfolio_value": 4000,
    }
    client._positions = {
        "market_positions": [
            {
                "ticker": "KXDEMO-COIN",
                "position_fp": "200.00",
                "market_exposure_dollars": "100.00",
            }
        ]
    }
    client._book = {
        "orderbook_fp": {
            "yes_dollars": [["0.1900", "80"]],
            "no_dollars": [["0.7900", "80"]],
        }
    }
    client.last_trade = lambda slug: Decimal("0.20")  # type: ignore[method-assign]
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
            now=now,
        )
    finally:
        logger.close()
    assert state["daily_loss_limit_hit"] is True
    assert state["quotes_placed"] == 0
    assert "daily_loss_limit" in (tmp_path / "demo.jsonl").read_text()


def test_demo_state_written_each_tick(tmp_path):
    cfg = load_config()
    client = _DemoFake()
    logger = DecisionLogger(tmp_path / "demo.jsonl")
    try:
        run_demo_session(
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
    live = cfg.logging.demo_state_path
    assert live.exists()
    import json

    payload = json.loads(live.read_text())
    assert payload["demo"] is True
    assert payload["running"] is True
    assert payload["tick"] >= 1


def test_demo_session_stops_on_signal_and_cancels(tmp_path):
    from polymarket_bot.demo.session import request_demo_stop

    cfg = load_config()
    client = _DemoFake()
    orig = client.place_demo_order

    def place(**kwargs):
        out = orig(**kwargs)
        request_demo_stop()
        return out

    client.place_demo_order = place  # type: ignore[method-assign]
    logger = DecisionLogger(tmp_path / "demo.jsonl")
    now = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)
    try:
        state = run_demo_session(
            client,
            cfg,
            logger,
            confirm_demo=True,
            ticks=8,
            ticker="KXDEMO-COIN",
            sleep=False,
            now=now,
        )
    finally:
        logger.close()
    assert state["stopped_by_signal"] is True
    assert state["quotes_placed"] >= 1
    assert state["quotes_placed"] < 16
    assert client.shutdowns == 1
    assert "demo_stop" in (tmp_path / "demo.jsonl").read_text()


def test_sigint_sets_stop_flag_without_traceback():
    import os
    import signal

    from polymarket_bot.demo.session import (
        demo_stop_requested,
        install_demo_signal_handlers,
        reset_demo_stop,
        restore_demo_signal_handlers,
    )

    reset_demo_stop()
    install_demo_signal_handlers()
    try:
        os.kill(os.getpid(), signal.SIGINT)
        assert demo_stop_requested() is True
    finally:
        restore_demo_signal_handlers()
        reset_demo_stop()
