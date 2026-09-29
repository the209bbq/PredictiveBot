from decimal import Decimal

from polymarket_bot.config import load_config
from polymarket_bot.market_data import MarketSnapshot
from polymarket_bot.paper.maker import desired_quotes
from polymarket_bot.paper.near_resolution import desired_quotes as near_quotes
from polymarket_bot.paper.portfolio import Portfolio


def _snap(
    *,
    bid="0.50",
    ask="0.51",
    tick="0.01",
    hours=48,
    last="0.50",
    stale=False,
    book_fetched=True,
    slug="m",
    venue="kalshi",
) -> MarketSnapshot:
    bid_d, ask_d = Decimal(bid), Decimal(ask)
    return MarketSnapshot(
        slug=slug,
        question="q",
        category="other",
        status="open",
        end_date=None,
        hours_to_resolution=hours,
        best_bid=bid_d,
        best_ask=ask_d,
        mid=(bid_d + ask_d) / 2,
        spread=ask_d - bid_d,
        last_trade=Decimal(last),
        volume_shares=Decimal("1000"),
        open_interest=Decimal("1000"),
        notional_traded=None,
        bid_depth_contracts=Decimal("50"),
        ask_depth_contracts=Decimal("50"),
        tick_size=Decimal(tick),
        fee_coefficient=Decimal("0.07"),
        venue=venue,
        fee_type="quadratic",
        stale=stale,
        book_fetched=book_fetched,
    )


def test_join_touch_on_one_tick_book():
    cfg = load_config()
    port = Portfolio("maker", Decimal("1000"), Decimal("1000"))
    quotes = desired_quotes(_snap(bid="0.50", ask="0.51", tick="0.01"), port, cfg, "t0")
    prices = {q.side: q.price for q in quotes}
    assert prices["buy"] == Decimal("0.50")
    assert prices["sell"] == Decimal("0.51")


def test_improve_when_spread_is_three_ticks():
    cfg = load_config()
    port = Portfolio("maker", Decimal("1000"), Decimal("1000"))
    quotes = desired_quotes(_snap(bid="0.48", ask="0.51", tick="0.01"), port, cfg, "t0")
    prices = {q.side: q.price for q in quotes}
    assert prices["buy"] == Decimal("0.49")
    assert prices["sell"] == Decimal("0.50")
    assert prices["buy"] < Decimal("0.51")
    assert prices["sell"] > Decimal("0.48")


def test_never_cross_or_lock():
    cfg = load_config()
    port = Portfolio("maker", Decimal("1000"), Decimal("1000"))
    quotes = desired_quotes(_snap(bid="0.50", ask="0.51", tick="0.01"), port, cfg, "t0")
    for q in quotes:
        if q.side == "buy":
            assert q.price < Decimal("0.51")
        else:
            assert q.price > Decimal("0.50")


def test_account_risk_cap_blocks_quote():
    cfg = load_config()
    object.__setattr__(
        cfg.paper.risk,
        "max_account_risk_pct",
        Decimal("0.01"),
    )
    port = Portfolio("maker", Decimal("10"), Decimal("10"))
    quotes = desired_quotes(_snap(bid="0.50", ask="0.51", tick="0.01"), port, cfg, "t0")
    # 10 contracts * 0.50 = $5 on $10 equity = 50% > 1%
    assert quotes == []


def test_stale_book_not_quoted():
    cfg = load_config()
    port = Portfolio("maker", Decimal("1000"), Decimal("1000"))
    assert desired_quotes(_snap(stale=True), port, cfg, "t0") == []
    assert desired_quotes(_snap(book_fetched=False), port, cfg, "t0") == []
    assert near_quotes(_snap(bid="0.96", ask="0.97", hours=12, stale=True), port, cfg, "t0") == []
