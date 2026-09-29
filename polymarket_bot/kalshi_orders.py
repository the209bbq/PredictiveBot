"""Kalshi Create Order V2 (POST /portfolio/events/orders) mapping.

The V2 book is YES-only: `bid` buys YES, `ask` sells YES. Buying NO at p is
an ask at 1-p. Legacy `/portfolio/orders` used side=yes|no; this client stays
on V2. post_only is unchanged.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

from polymarket_bot.paper.portfolio import PaperOrder

# Keep in sync with polymarket_bot.exchanges.kalshi.CLIENT_ORDER_PREFIX.
CLIENT_ORDER_PREFIX = "pmbot-"

ONE = Decimal("1")
ZERO = Decimal("0")


def format_v2_price(price: Decimal) -> str:
    return f"{Decimal(price):.4f}"


def format_v2_count(count: Decimal | int | str) -> str:
    return str(int(Decimal(str(count))))


def v2_book_order(*, contract_side: str, action: str, price: Decimal) -> tuple[str, Decimal]:
    """Return (book_side, yes_price) for CreateOrderV2Request.

    bid = buy YES; ask = sell YES. Buy NO at p → ask at 1-p.
    """
    side = (contract_side or "yes").lower()
    act = (action or "buy").lower()
    px = Decimal(price)
    if act in {"buy", "bid", "long"}:
        if side == "no":
            return "ask", ONE - px
        return "bid", px
    if act in {"sell", "ask", "short"}:
        if side == "no":
            return "bid", ONE - px
        return "ask", px
    raise ValueError(f"unknown action {action!r}")


def v2_from_paper_order(order: PaperOrder) -> tuple[str, Decimal]:
    contract = getattr(order, "contract_side", None) or "yes"
    return v2_book_order(contract_side=contract, action=order.side, price=order.price)


def v2_would_cross(book_side: str, yes_price: Decimal, yes_bid: Decimal | None, yes_ask: Decimal | None) -> bool:
    """True if a V2 post-only order at yes_price would take the YES book."""
    if book_side == "bid":
        return yes_ask is not None and yes_price >= yes_ask
    if book_side == "ask":
        return yes_bid is not None and yes_price <= yes_bid
    return False


def long_no_at_risk(no_price: Decimal, qty: Decimal) -> Decimal:
    """Worst-case $ if a long-NO (buy NO / sell YES) fill expires YES."""
    return abs(Decimal(no_price) * Decimal(qty))


def create_order_v2_body(
    *,
    ticker: str,
    side: str,
    price: str | Decimal,
    count: str | Decimal,
    post_only: bool = True,
    client_order_id: str | None = None,
) -> dict[str, Any]:
    """Exact CreateOrderV2Request fields we send (documented schema)."""
    token = str(side).lower()
    px = Decimal(str(price))
    if token in {"yes", "no"}:
        book_side, px = v2_book_order(contract_side=token, action="buy", price=px)
    elif token in {"bid", "ask"}:
        book_side = token
    else:
        raise ValueError(f"side must be bid, ask, yes, or no; got {side!r}")
    if px <= ZERO or px >= ONE:
        raise ValueError(f"V2 yes-price {px} is outside (0, 1)")
    cid = client_order_id or f"{CLIENT_ORDER_PREFIX}{uuid4().hex}"
    return {
        "ticker": ticker,
        "side": book_side,
        "count": format_v2_count(count),
        "price": format_v2_price(px),
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": post_only,
        "client_order_id": cid,
    }
