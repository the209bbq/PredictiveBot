"""Maker-only quotes around mid, inventory-skewed."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

from polymarket_bot.config import AppConfig
from polymarket_bot.fees import MAX_PRICE, MIN_PRICE
from polymarket_bot.market_data import MarketSnapshot
from polymarket_bot.paper.portfolio import PaperOrder, Portfolio
from polymarket_bot.paper.risk import past_resolution_cutoff, would_breach_position

STRATEGY = "maker"


def round_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return price
    steps = (price / tick).to_integral_value(rounding=ROUND_HALF_EVEN)
    return steps * tick


def clamp_price(price: Decimal, tick: Decimal) -> Decimal | None:
    px = round_to_tick(price, tick)
    if px < MIN_PRICE or px > MAX_PRICE:
        return None
    return px


def desired_quotes(
    snap: MarketSnapshot,
    portfolio: Portfolio,
    config: AppConfig,
    order_id_prefix: str,
) -> list[PaperOrder]:
    maker = config.paper.maker
    if not maker.enabled:
        return []
    if snap.mid is None or snap.best_bid is None or snap.best_ask is None:
        return []
    if past_resolution_cutoff(snap, config.paper.risk.maker_min_hours_to_resolution):
        return []

    pos = portfolio.position(snap.slug).qty
    skew = pos * maker.inventory_skew_per_contract
    # Long inventory → shade both quotes down so we are more likely to sell.
    raw_bid = snap.mid - maker.half_spread - skew
    raw_ask = snap.mid + maker.half_spread - skew
    tick = snap.tick_size or config.paper.tick_size_fallback
    bid = clamp_price(raw_bid, tick)
    ask = clamp_price(raw_ask, tick)
    if bid is None or ask is None or bid >= ask:
        return []
    # Stay maker: never join or cross the opposite side.
    if bid >= snap.best_ask or ask <= snap.best_bid:
        return []

    qty = config.paper.quote_size_contracts
    orders: list[PaperOrder] = []
    for side, price in (("buy", bid), ("sell", ask)):
        if would_breach_position(portfolio, snap.slug, side, qty, config.paper.risk):
            continue
        if not portfolio.buying_power_ok(side, price, qty):
            continue
        orders.append(
            PaperOrder(
                order_id=f"{order_id_prefix}-{snap.slug}-{side}",
                market=snap.slug,
                side=side,
                price=price,
                qty=qty,
                strategy=STRATEGY,
            )
        )
    return orders
