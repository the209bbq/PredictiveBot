from datetime import datetime, timezone
from decimal import Decimal

from polymarket_bot.config import load_config
from polymarket_bot.guard import LiveTradingDisabled, assert_paper_only
from polymarket_bot.live import start_live_trading
from polymarket_bot.logging_utils import DecisionLogger
from polymarket_bot.market_data.replay_client import ReplayClient
from polymarket_bot.paper.engine import run_paper
from polymarket_bot.paper.portfolio import PaperOrder, Portfolio
from polymarket_bot.paper.report import format_report


def test_live_stub_raises():
    try:
        start_live_trading()
        raise AssertionError("should have raised")
    except LiveTradingDisabled:
        pass


def test_assert_paper_only_raises_when_flag_on():
    try:
        assert_paper_only(dry_run=True, live_trading_enabled=True, env_flags=[None])
        raise AssertionError("should have raised")
    except LiveTradingDisabled:
        pass
    try:
        assert_paper_only(dry_run=False, live_trading_enabled=False, env_flags=[None])
        raise AssertionError("should have raised")
    except LiveTradingDisabled:
        pass
    try:
        assert_paper_only(dry_run=True, live_trading_enabled=False, env_flags=["true"])
        raise AssertionError("should have raised")
    except LiveTradingDisabled:
        pass
    assert_paper_only(dry_run=True, live_trading_enabled=False, env_flags=["false"])


def test_maker_fill_applies_rebate_and_inventory():
    port = Portfolio("maker", Decimal("1000"), Decimal("1000"))
    order = PaperOrder("1", "m", "buy", Decimal("0.50"), Decimal("10"), "maker")
    fill = port.apply_fill(order, Decimal("10"), "trade_through")
    assert fill.rebate == Decimal("0.03")  # 0.0125 * 10 * 0.25 = 0.03125 → 0.03
    assert port.position("m").qty == Decimal("10")
    assert port.cash == Decimal("1000") - Decimal("5.00") + fill.rebate


def test_replay_paper_run_produces_fills_and_report(tmp_path):
    cfg = load_config()
    now = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)
    client = ReplayClient("fixtures/replay.json", now=now)
    logger = DecisionLogger(tmp_path / "decisions.jsonl")
    try:
        state = run_paper(client, cfg, logger, ticks=3, sleep=False, now=now)
    finally:
        logger.close()
        client.close()
    assert state["dry_run"] is True
    assert state["live"] is False
    assert int(state["maker"]["fill_count"]) >= 1
    assert int(state["near_resolution"]["fill_count"]) >= 1
    report = format_report(state)
    assert "DRY-RUN ONLY" in report
    assert "Maker-only" in report
    assert "Near-resolution" in report
    assert "Net P&L" in report
    assert "Max drawdown" in report
