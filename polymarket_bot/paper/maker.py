"""Maker-only quotes: join or improve the touch, inventory-skewed."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

from polymarket_bot.config import AppConfig
from polymarket_bot.fees import MAX_PRICE, MIN_PRICE
from polymarket_bot.market_data import MarketSnapshot
from polymarket_bot.paper.portfolio import PaperOrder, Portfolio
from polymarket_bot.paper.risk import (
    account_value_from_portfolio,
    past_resolution_cutoff,
    would_breach_account_risk,
    would_breach_position,
)

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
    resting: list[PaperOrder] | None = None,
) -> list[PaperOrder]:
    maker = config.paper.maker
    if not maker.enabled:
        return []
    if snap.stale or not snap.book_fetched:
        return []
    if snap.mid is None or snap.best_bid is None or snap.best_ask is None:
        return []
    if past_resolution_cutoff(snap, config.paper.risk.maker_min_hours_to_resolution):
        return []

    tick = snap.tick_size or config.paper.tick_size_fallback
    if tick <= 0:
        return []
    pos = portfolio.position(snap.slug).qty
    skew = pos * maker.inventory_skew_per_contract
    improve = tick * Decimal(max(0, maker.improve_ticks))
    spread = snap.best_ask - snap.best_bid

    # Join the touch on a one-tick book; improve when there is room.
    if spread >= tick * 3:
        raw_bid = snap.best_bid + improve
        raw_ask = snap.best_ask - improve
    else:
        raw_bid = snap.best_bid
        raw_ask = snap.best_ask
    raw_bid -= skew
    raw_ask -= skew

    # Stay inside half_spread of mid so we do not chase a wide book.
    raw_bid = max(raw_bid, snap.mid - maker.half_spread)
    raw_ask = min(raw_ask, snap.mid + maker.half_spread)

    bid = clamp_price(raw_bid, tick)
    ask = clamp_price(raw_ask, tick)
    if bid is not None and ask is not None and bid >= ask:
        return []

    qty = config.paper.quote_size_contracts
    booked = list(resting or [])
    equity = account_value_from_portfolio(portfolio, {snap.slug: snap.mid})
    cap = config.paper.risk.max_account_risk_pct
    orders: list[PaperOrder] = []
    for side, price in (("buy", bid), ("sell", ask)):
        if price is None:
            continue
        # Maker-only: never cross or lock the opposite side.
        if side == "buy" and price >= snap.best_ask:
            continue
        if side == "sell" and price <= snap.best_bid:
            continue
        if would_breach_position(portfolio, snap.slug, side, qty, config.paper.risk):
            continue
        if not portfolio.buying_power_ok(side, price, qty):
            continue
        candidate = PaperOrder(
            order_id=f"{order_id_prefix}-{snap.slug}-{side}",
            market=snap.slug,
            side=side,
            price=price,
            qty=qty,
            strategy=STRATEGY,
            venue=snap.venue,
            fee_type=snap.fee_type,
            fee_multiplier=snap.fee_multiplier,
        )
        if would_breach_account_risk(portfolio, booked, candidate, equity, cap):
            continue
        orders.append(candidate)
        booked.append(candidate)
    return orders
