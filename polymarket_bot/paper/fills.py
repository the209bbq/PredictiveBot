"""Conservative maker fill simulation.

A resting quote fills only when the market *trades through* it:

* Buy (bid): a tape print < bid, or the best ask crossed strictly through.
* Sell (ask): a tape print > ask, or the best bid crossed strictly through.

Prints *at* the quote consume queue-ahead first (back of the queue).
Stale last trades that already sat through the quote do not fill again.
When only a single last-trade print is available, fill size is 1 contract.
"""

from __future__ import annotations

from decimal import Decimal

from polymarket_bot.market_data import MarketSnapshot, TapeTrade
from polymarket_bot.paper.portfolio import PaperOrder

ZERO = Decimal("0")
ONE = Decimal("1")


def last_trade_changed(prev: MarketSnapshot | None, cur: MarketSnapshot) -> bool:
    if prev is None:
        return False
    if cur.last_trade is None or prev.last_trade is None:
        return False
    return cur.last_trade != prev.last_trade


def _yes_space(order: PaperOrder) -> tuple[str, Decimal]:
    """Map a favorite-side contract onto the YES book used by the tape.

    Buying NO at p is economically a YES sell at 1-p. The public tape is YES.
    """
    side = getattr(order, "contract_side", "yes") or "yes"
    if side == "no":
        flipped = "sell" if order.side == "buy" else "buy"
        return flipped, ONE - order.price
    return order.side, order.price


def queue_ahead(order: PaperOrder, snap: MarketSnapshot) -> Decimal:
    yes_side, yes_price = _yes_space(order)
    levels = snap.bids if yes_side == "buy" else snap.asks
    for lvl in levels:
        if lvl.price == yes_price:
            return lvl.qty
    return ZERO


def synthesize_tape(cur: MarketSnapshot, prev: MarketSnapshot | None) -> list[TapeTrade]:
    if cur.tape:
        return list(cur.tape)
    if last_trade_changed(prev, cur) and cur.last_trade is not None:
        return [TapeTrade(price=cur.last_trade, qty=ONE)]
    return []


def _tape_fill(
    order: PaperOrder,
    tape: list[TapeTrade],
    snap: MarketSnapshot,
    *,
    strict: bool,
    use_queue: bool,
) -> tuple[Decimal, str | None]:
    filled = ZERO
    queue = queue_ahead(order, snap) if use_queue else ZERO
    for trade in tape:
        if filled >= order.qty:
            break
        yes_side, yes_price = _yes_space(order)
        if yes_side == "buy":
            through = trade.price < yes_price
            at_px = trade.price == yes_price
        else:
            through = trade.price > yes_price
            at_px = trade.price == yes_price
        if through:
            take = min(order.qty - filled, trade.qty)
            if take > 0:
                filled += take
        elif at_px:
            if strict and use_queue:
                leftover = trade.qty - queue
                queue = max(ZERO, queue - trade.qty)
                if leftover > 0:
                    filled += min(order.qty - filled, leftover)
            elif not strict:
                filled += min(order.qty - filled, trade.qty)
    if filled > 0:
        return filled, "trade_through"
    return ZERO, None


def fill_qty(
    order: PaperOrder,
    cur: MarketSnapshot,
    prev: MarketSnapshot | None,
    *,
    strict: bool = True,
) -> tuple[Decimal, str | None]:
    if cur.stale:
        return ZERO, None
    tape = list(cur.tape or [])
    use_queue = bool(tape)
    if not tape:
        tape = synthesize_tape(cur, prev)
    if tape:
        qty, reason = _tape_fill(order, tape, cur, strict=strict, use_queue=use_queue)
        if reason:
            return qty, reason
    book = _book_cross(order, cur, prev, strict=strict)
    if book:
        return order.qty, book
    return ZERO, None


def _book_cross(
    order: PaperOrder,
    cur: MarketSnapshot,
    prev: MarketSnapshot | None,
    *,
    strict: bool,
) -> str | None:
    if prev is None:
        return None
    yes_side, yes_price = _yes_space(order)
    if yes_side == "buy" and cur.best_ask is not None:
        was_above = prev.best_ask is None or prev.best_ask >= yes_price
        crossed = (cur.best_ask < yes_price) if strict else (cur.best_ask <= yes_price)
        if was_above and crossed:
            return "book_cross"
        return None
    if yes_side == "sell" and cur.best_bid is not None:
        was_below = prev.best_bid is None or prev.best_bid <= yes_price
        crossed = (cur.best_bid > yes_price) if strict else (cur.best_bid >= yes_price)
        if was_below and crossed:
            return "book_cross"
    return None


def fill_reason(
    order: PaperOrder,
    cur: MarketSnapshot,
    prev: MarketSnapshot | None,
    *,
    strict: bool = True,
) -> str | None:
    _qty, reason = fill_qty(order, cur, prev, strict=strict)
    return reason
