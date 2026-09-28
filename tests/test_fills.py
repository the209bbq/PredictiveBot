from decimal import Decimal

from polymarket_bot.market_data import MarketSnapshot
from polymarket_bot.paper.fills import fill_reason
from polymarket_bot.paper.portfolio import PaperOrder


def _snap(last, bid, ask) -> MarketSnapshot:
    last_d = Decimal(str(last)) if last is not None else None
    bid_d = Decimal(str(bid)) if bid is not None else None
    ask_d = Decimal(str(ask)) if ask is not None else None
    mid = None
    spread = None
    if bid_d is not None and ask_d is not None:
        mid = (bid_d + ask_d) / 2
        spread = ask_d - bid_d
    return MarketSnapshot(
        slug="m",
        question="q",
        category="other",
        status="MARKET_STATUS_OPEN",
        end_date=None,
        hours_to_resolution=48,
        best_bid=bid_d,
        best_ask=ask_d,
        mid=mid,
        spread=spread,
        last_trade=last_d,
        volume_shares=Decimal("1000"),
        open_interest=None,
        notional_traded=None,
        bid_depth_contracts=Decimal("50"),
        ask_depth_contracts=Decimal("50"),
        tick_size=Decimal("0.001"),
        fee_coefficient=Decimal("0.0695"),
    )


def _bid(price="0.48") -> PaperOrder:
    return PaperOrder("1", "m", "buy", Decimal(price), Decimal("10"), "maker")


def _ask(price="0.52") -> PaperOrder:
    return PaperOrder("2", "m", "sell", Decimal(price), Decimal("10"), "maker")


def test_no_fill_without_previous_tick():
    cur = _snap("0.47", "0.46", "0.50")
    assert fill_reason(_bid(), cur, None) is None


def test_stale_last_trade_does_not_fill():
    prev = _snap("0.47", "0.46", "0.50")
    cur = _snap("0.47", "0.46", "0.50")
    assert fill_reason(_bid(), cur, prev) is None


def test_new_trade_through_fills_bid():
    prev = _snap("0.50", "0.49", "0.51")
    cur = _snap("0.47", "0.46", "0.50")
    assert fill_reason(_bid(), cur, prev) == "trade_through"


def test_print_at_quote_does_not_fill_when_strict():
    prev = _snap("0.50", "0.49", "0.51")
    cur = _snap("0.48", "0.47", "0.50")
    assert fill_reason(_bid("0.48"), cur, prev, strict=True) is None
    assert fill_reason(_bid("0.48"), cur, prev, strict=False) == "trade_through"


def test_book_cross_fills_bid():
    prev = _snap("0.50", "0.49", "0.51")
    cur = _snap("0.50", "0.46", "0.47")
    assert fill_reason(_bid("0.48"), cur, prev) == "book_cross"


def test_new_trade_through_fills_ask():
    prev = _snap("0.50", "0.49", "0.51")
    cur = _snap("0.54", "0.53", "0.55")
    assert fill_reason(_ask(), cur, prev) == "trade_through"
