"""Conservative maker fill simulation.

A resting quote fills only when the market *trades through* it on this tick:

* Buy (bid): last trade moved this tick and last_trade < bid, or the
  best ask crossed strictly through the bid (best_ask < bid) this tick.
* Sell (ask): last trade moved this tick and last_trade > ask, or the
  best bid crossed strictly through the ask (best_bid > ask) this tick.

Prints *at* the quote are ignored. Stale last trades that already sat
through the quote do not fill again.
"""

from __future__ import annotations

from polymarket_bot.market_data import MarketSnapshot
from polymarket_bot.paper.portfolio import PaperOrder


def last_trade_changed(prev: MarketSnapshot | None, cur: MarketSnapshot) -> bool:
    if prev is None:
        return False
    if cur.last_trade is None or prev.last_trade is None:
        return False
    return cur.last_trade != prev.last_trade


def fill_reason(
    order: PaperOrder,
    cur: MarketSnapshot,
    prev: MarketSnapshot | None,
    *,
    strict: bool = True,
) -> str | None:
    if order.side == "buy":
        if last_trade_changed(prev, cur) and cur.last_trade is not None:
            if (cur.last_trade < order.price) if strict else (cur.last_trade <= order.price):
                return "trade_through"
        if prev is not None and cur.best_ask is not None:
            was_above = prev.best_ask is None or prev.best_ask >= order.price
            crossed = (cur.best_ask < order.price) if strict else (cur.best_ask <= order.price)
            if was_above and crossed:
                return "book_cross"
        return None

    if last_trade_changed(prev, cur) and cur.last_trade is not None:
        if (cur.last_trade > order.price) if strict else (cur.last_trade >= order.price):
            return "trade_through"
    if prev is not None and cur.best_bid is not None:
        was_below = prev.best_bid is None or prev.best_bid <= order.price
        crossed = (cur.best_bid > order.price) if strict else (cur.best_bid >= order.price)
        if was_below and crossed:
            return "book_cross"
    return None
