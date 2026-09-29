from datetime import datetime, timezone
from decimal import Decimal

from polymarket_bot.config import load_config
from polymarket_bot.exchanges.kalshi import snapshot_from_kalshi
from polymarket_bot.paper.maker import desired_quotes
from polymarket_bot.paper.portfolio import PaperOrder, Portfolio
from polymarket_bot.series_filter import (
    gas_blackout,
    is_kxhigh_same_day,
    maker_min_hours,
    maker_universe_ok,
    touch_queue,
)


def _kalshi(ticker, **fields):
    now = fields.pop("now", datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc))
    bid = fields.pop("bid", "0.49")
    ask = fields.pop("ask", "0.51")
    no_bid = f"{(Decimal('1') - Decimal(ask)):.4f}"
    market = {
        "ticker": ticker,
        "series_ticker": fields.pop("series", ticker.split("-", 1)[0]),
        "title": fields.pop("title", ticker),
        "status": "active",
        "yes_bid_dollars": bid,
        "yes_ask_dollars": ask,
        "close_time": fields.pop("close", "2026-10-01T00:00:00Z"),
        "fee_type": fields.pop("fee_type", "quadratic"),
        "category": fields.pop("category", "other"),
    }
    book = {
        "orderbook_fp": {
            "yes_dollars": [[f"{Decimal(bid):.4f}", "80"]],
            "no_dollars": [[no_bid, "80"]],
        }
    }
    return snapshot_from_kalshi(market, book, now=now)


def test_allow_demo_and_deny_sports():
    cfg = load_config()
    assert maker_universe_ok(_kalshi("KXDEMO-COIN"), cfg)
    assert not maker_universe_ok(_kalshi("KXNFLGAME-26SEP28PHICHI-CHI", series="KXNFLGAME"), cfg)
    assert not maker_universe_ok(_kalshi("KXNFLYARDS-COOPER", series="KXNFLYARDS", title="Cooper receiving yards"), cfg)


def test_kxhigh_same_day_off_by_default_opt_in():
    cfg = load_config()
    assert cfg.paper.risk.kxhigh_resolution_day_enabled is False
    late = _kalshi("KXHIGHNY-29SEP26", series="KXHIGHNY", close="2026-09-29T13:00:00Z")
    morning = _kalshi("KXHIGHNY-29SEP26", series="KXHIGHNY", close="2026-09-29T18:00:00Z")
    nxt = _kalshi("KXHIGHNY-30SEP26", series="KXHIGHNY", close="2026-09-30T18:00:00Z")
    assert not maker_universe_ok(late, cfg)
    assert morning.hours_to_resolution is not None and morning.hours_to_resolution < 24
    assert not maker_universe_ok(morning, cfg)
    assert nxt.hours_to_resolution is not None and nxt.hours_to_resolution >= 24
    assert maker_universe_ok(nxt, cfg)
    assert maker_min_hours(morning, cfg) == 24.0
    cfg.extra["paper"]["series"]["kxhigh_resolution_day_enabled"] = True
    assert maker_universe_ok(morning, cfg)
    assert maker_min_hours(morning, cfg) == 4.0


def test_kxrt_skips_extremes_and_btc15m_denied():
    cfg = load_config()
    extreme = _kalshi("KXRT-FOO", series="KXRT", bid="0.01", ask="0.02")
    mid = _kalshi("KXRT-FOO", series="KXRT")
    assert not maker_universe_ok(extreme, cfg)
    assert maker_universe_ok(mid, cfg)
    assert not maker_universe_ok(_kalshi("KXBTC15M-A", series="KXBTC15M"), cfg)
    assert not maker_universe_ok(_kalshi("KXBTC-RANGE-50K", series="KXBTC"), cfg)
    assert maker_universe_ok(_kalshi("KXBRENTW-OCT", series="KXBRENTW"), cfg)
    assert maker_universe_ok(_kalshi("KXU3-OCT", series="KXU3", close="2026-11-06T12:30:00Z"), cfg)
    assert maker_universe_ok(
        _kalshi("KXPAYROLLS-OCT", series="KXPAYROLLS", close="2026-11-06T12:30:00Z"), cfg
    )
    cfg.extra["paper"]["series"]["allow"].append("KXBTC")
    assert maker_universe_ok(_kalshi("KXBTC-RANGE-50K", series="KXBTC"), cfg)


def test_skip_maker_fee_series_and_jobs_blackout():
    cfg = load_config()
    fee = _kalshi("KXRT-FEE", series="KXRT", fee_type="quadratic_with_maker_fees")
    assert not maker_universe_ok(fee, cfg)
    during_jobs = datetime(2026, 10, 2, 12, 30, tzinfo=timezone.utc)
    rt = _kalshi("KXRT-JOBS", series="KXRT", now=during_jobs, close="2026-10-10T00:00:00Z")
    assert not maker_universe_ok(rt, cfg, now=during_jobs)
    later = datetime(2026, 10, 2, 18, 0, tzinfo=timezone.utc)
    assert maker_universe_ok(rt, cfg, now=later)


def test_touch_queue_and_maker_skips_denied():
    cfg = load_config()
    snap = _kalshi("KXNFLGAME-X", series="KXNFLGAME")
    assert touch_queue(snap) == Decimal("80")
    port = Portfolio("maker", Decimal("1000"), Decimal("1000"))
    assert desired_quotes(snap, port, cfg, "t0") == []


def test_gas_blackout_evening_daily_and_morning_weekly():
    cfg = load_config()
    daily = _kalshi("KXAAAGASD-30SEP26", series="KXAAAGASD", close="2026-09-30T04:00:00Z")
    weekly = _kalshi("KXAAAGASW-05OCT26", series="KXAAAGASW", close="2026-10-05T13:00:00Z")
    evening = datetime(2026, 9, 29, 3, 30, tzinfo=timezone.utc)  # 20:30 PT Sep 28
    morning = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)  # 05:00 PT
    midday = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc)  # 13:00 PT
    assert gas_blackout(daily, cfg, evening) is True
    assert gas_blackout(weekly, cfg, evening) is False
    assert gas_blackout(daily, cfg, morning) is True
    assert gas_blackout(weekly, cfg, morning) is True
    assert gas_blackout(daily, cfg, midday) is False
    assert gas_blackout(weekly, cfg, midday) is False


def test_kxhigh_same_day_fill_is_tagged():
    morning = _kalshi("KXHIGHNY-29SEP26", series="KXHIGHNY", close="2026-09-29T18:00:00Z")
    assert is_kxhigh_same_day(morning) is True
    port = Portfolio("maker", Decimal("1000"), Decimal("1000"))
    fill = port.apply_fill(
        PaperOrder("1", morning.slug, "buy", Decimal("0.50"), Decimal("1"), "maker"),
        Decimal("1"),
        "trade_through",
        same_day=True,
    )
    assert fill.same_day is True
    assert port.to_dict()["same_day_fills"] == 1
    assert port.to_dict()["fills"][0]["same_day"] is True
