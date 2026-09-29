from datetime import datetime, timezone
from decimal import Decimal

from polymarket_bot.config import TradingConfig, load_config
from polymarket_bot.daily_limits import (
    daily_limits_path,
    empty_limits,
    pt_date,
    refresh_daily_limits,
    save_store,
)
from polymarket_bot.logging_utils import DecisionLogger
from polymarket_bot.market_data.replay_client import ReplayClient
from polymarket_bot.paper.engine import run_paper
from polymarket_bot.paper.maker import desired_quotes
from polymarket_bot.paper.portfolio import PaperOrder, Portfolio
from polymarket_bot.paper.risk import would_breach_risk_limits


def _cfg(tmp_path):
    cfg = load_config()
    object.__setattr__(
        cfg,
        "trading",
        TradingConfig(True, tmp_path / "toggle.json", tmp_path / "lock"),
    )
    return cfg


def test_daily_loss_triggers_and_resets_at_pt_midnight(tmp_path):
    cfg = _cfg(tmp_path)
    afternoon = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc)  # 13:00 PT
    first = refresh_daily_limits(cfg, equity=Decimal("1000"), capital_in_use=Decimal("0"), now=afternoon)
    assert first.loss_halted is False
    assert pt_date(afternoon) == "2026-09-29"

    hit = refresh_daily_limits(cfg, equity=Decimal("949"), capital_in_use=Decimal("10"), now=afternoon)
    assert hit.loss_halted is True
    assert hit.just_triggered is True
    assert hit.day_pnl == Decimal("-51")

    still = refresh_daily_limits(cfg, equity=Decimal("960"), capital_in_use=Decimal("10"), now=afternoon)
    assert still.loss_halted is True
    assert still.just_triggered is False

    after_midnight = datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc)  # 01:00 PT Sep 30
    reset = refresh_daily_limits(
        cfg, equity=Decimal("960"), capital_in_use=Decimal("10"), now=after_midnight
    )
    assert reset.loss_halted is False
    assert reset.pt_date == "2026-09-30"
    assert reset.start_equity == Decimal("960")


def test_winning_day_has_no_pnl_cap(tmp_path):
    cfg = _cfg(tmp_path)
    now = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc)
    refresh_daily_limits(cfg, equity=Decimal("1000"), capital_in_use=Decimal("0"), now=now)
    up = refresh_daily_limits(cfg, equity=Decimal("5000"), capital_in_use=Decimal("20"), now=now)
    assert up.loss_halted is False
    assert up.day_pnl == Decimal("4000")


def test_empty_limits_resets_halt_after_pt_midnight(tmp_path):
    cfg = _cfg(tmp_path)
    afternoon = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc)
    refresh_daily_limits(cfg, equity=Decimal("940"), capital_in_use=Decimal("10"), now=afternoon)
    assert empty_limits(cfg, now=afternoon).loss_halted is True
    after = datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc)
    rolled = empty_limits(cfg, now=after)
    assert rolled.loss_halted is False
    assert rolled.pt_date == "2026-09-30"
    assert rolled.day_pnl == Decimal("0")


def test_default_dollar_limits():
    cfg = load_config()
    assert cfg.paper.risk.max_daily_loss_usd == Decimal("50")
    assert cfg.paper.risk.max_daily_capital_in_use_usd == Decimal("100")
    demo = load_config(environment="demo")
    live = load_config(environment="live")
    assert demo.paper.risk.max_daily_loss_usd == Decimal("50")
    assert demo.paper.risk.max_daily_capital_in_use_usd == Decimal("100")
    assert live.paper.risk.max_daily_loss_usd == Decimal("50")
    assert live.paper.risk.max_daily_capital_in_use_usd == Decimal("100")


def test_capital_cap_blocks_quote():
    cfg = load_config()
    object.__setattr__(cfg.paper.risk, "max_daily_capital_in_use_usd", Decimal("0.10"))
    port = Portfolio("maker", Decimal("1000"), Decimal("1000"))
    from tests.test_maker import _snap

    assert desired_quotes(_snap(bid="0.50", ask="0.51", hours=48), port, cfg, "t0") == []


def test_tighter_of_pct_and_dollar_cap_wins():
    cfg = load_config()
    port = Portfolio("maker", Decimal("1000"), Decimal("1000"))
    order = PaperOrder("1", "m", "buy", Decimal("0.50"), Decimal("1"), "maker")
    # $100 is tighter than 40% of $1000 ($400)
    assert (
        would_breach_risk_limits(port, [], order, Decimal("1000"), cfg.paper.risk) is None
    )
    fat = PaperOrder("2", "m", "buy", Decimal("0.50"), Decimal("250"), "maker")
    assert (
        would_breach_risk_limits(port, [], fat, Decimal("1000"), cfg.paper.risk)
        == "daily_capital_cap"
    )
    # 40% of $10 is $4; a $5 buy is blocked by the percent cap
    small_acct = PaperOrder("3", "m", "buy", Decimal("0.50"), Decimal("10"), "maker")
    assert (
        would_breach_risk_limits(port, [], small_acct, Decimal("10"), cfg.paper.risk)
        == "account_risk_cap"
    )


def test_paper_cancels_when_daily_loss_already_hit(tmp_path):
    cfg = _cfg(tmp_path)
    now = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)
    save_store(
        daily_limits_path(cfg),
        {"pt_date": pt_date(now), "start_equity": "2000", "loss_halted": True},
    )
    client = ReplayClient("fixtures/replay.json", now=now)
    logger = DecisionLogger(tmp_path / "d.jsonl")
    try:
        state = run_paper(client, cfg, logger, ticks=2, sleep=False, now=now)
    finally:
        logger.close()
        client.close()
    text = (tmp_path / "d.jsonl").read_text()
    assert "daily_loss_limit" in text
    assert state["daily_loss_limit_hit"] is True
    assert int(state["maker"]["fill_count"]) == 0
