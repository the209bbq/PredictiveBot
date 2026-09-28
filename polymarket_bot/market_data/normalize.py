"""Build MarketSnapshot objects from list + book/bbo payloads."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from polymarket_bot.market_data import BookLevel, MarketSnapshot, as_datetime, as_decimal


def _levels(rows: Any) -> list[BookLevel]:
    out: list[BookLevel] = []
    for row in rows or []:
        price = as_decimal(row.get("px") if isinstance(row, dict) else getattr(row, "px", None))
        qty = as_decimal(row.get("qty") if isinstance(row, dict) else getattr(row, "qty", None))
        if price is None or qty is None:
            continue
        out.append(BookLevel(price=price, qty=qty))
    return out


def hours_to_resolution(end_date: datetime | None, now: datetime | None = None) -> float | None:
    if end_date is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (end_date - now).total_seconds() / 3600.0


def depth_from_levels(levels: list[BookLevel]) -> Decimal:
    return sum((lvl.qty for lvl in levels), Decimal("0"))


def snapshot_from_payloads(
    market: dict[str, Any],
    book: dict[str, Any] | None = None,
    bbo: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
    tick_size_fallback: Decimal = Decimal("0.001"),
) -> MarketSnapshot:
    md = (book or {}).get("marketData") or {}
    bb = (bbo or {}).get("marketData") or {}
    stats = md.get("stats") or {}

    bids = _levels(md.get("bids"))
    asks = _levels(md.get("offers") or md.get("asks"))

    best_bid = as_decimal(bb.get("bestBid")) or as_decimal(market.get("bestBidQuote"))
    best_ask = as_decimal(bb.get("bestAsk")) or as_decimal(market.get("bestAskQuote"))
    if best_bid is None and bids:
        best_bid = bids[0].price
    if best_ask is None and asks:
        best_ask = asks[0].price

    last_trade = (
        as_decimal(bb.get("lastTradePx"))
        or as_decimal(stats.get("lastTradePx"))
        or as_decimal(market.get("lastTradePrice"))
    )
    volume = as_decimal(bb.get("sharesTraded")) or as_decimal(stats.get("sharesTraded"))
    oi = as_decimal(bb.get("openInterest")) or as_decimal(stats.get("openInterest"))
    notional = as_decimal(stats.get("notionalTraded"))

    bid_depth = as_decimal(bb.get("bidShares")) or depth_from_levels(bids)
    ask_depth = as_decimal(bb.get("askShares")) or depth_from_levels(asks)
    if bid_depth is None:
        bid_depth = Decimal("0")
    if ask_depth is None:
        ask_depth = Decimal("0")

    mid = None
    spread = None
    if best_bid is not None and best_ask is not None:
        mid = (best_bid + best_ask) / Decimal("2")
        spread = best_ask - best_bid

    end_date = as_datetime(market.get("endDate"))
    tick = as_decimal(market.get("orderPriceMinTickSize")) or tick_size_fallback
    slug = str(market.get("slug") or md.get("marketSlug") or bb.get("marketSlug") or "")
    question = str(market.get("question") or market.get("title") or slug)

    return MarketSnapshot(
        slug=slug,
        question=question,
        category=market.get("category"),
        status=str(market.get("status") or md.get("state") or bb.get("state") or ""),
        end_date=end_date,
        hours_to_resolution=hours_to_resolution(end_date, now),
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        spread=spread,
        last_trade=last_trade,
        volume_shares=volume,
        open_interest=oi,
        notional_traded=notional,
        bid_depth_contracts=bid_depth,
        ask_depth_contracts=ask_depth,
        tick_size=tick,
        fee_coefficient=as_decimal(market.get("feeCoefficient")),
        bids=bids,
        asks=asks,
        raw={"market": market, "book": book, "bbo": bbo},
    )
