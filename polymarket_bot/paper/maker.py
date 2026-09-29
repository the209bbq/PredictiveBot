"""Maker-only quotes: join or improve the touch, inventory-skewed."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from polymarket_bot.config import AppConfig
from polymarket_bot.fees import MAX_PRICE, MIN_PRICE
from polymarket_bot.market_data import MarketSnapshot
from polymarket_bot.paper.portfolio import PaperOrder, Portfolio
from polymarket_bot.market_risk import attach_market_risk, is_live_in_game, market_over_risk_threshold
from polymarket_bot.series_filter import maker_min_hours, maker_universe_ok
from polymarket_bot.paper.risk import (
    account_value_from_portfolio,
    past_resolution_cutoff,
    would_breach_position,
    would_breach_risk_limits,
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
    if is_live_in_game(snap):
        return []
    if not maker_universe_ok(snap, config):
        return []
    if past_resolution_cutoff(snap, maker_min_hours(snap, config)):
        return []
    if snap.risk_score is None:
        attach_market_risk(snap)
    if market_over_risk_threshold(snap, config.paper.risk.max_market_risk_score):
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
    # 1-tick books: skew can round the unwind side onto the opposite price
    # and get dropped, leaving only the adding quote. Clamp unwind to the
    # best price on its own side instead of dropping it.
    if pos > 0 and (ask is None or ask <= snap.best_bid):
        ask = snap.best_ask
    if pos < 0 and (bid is None or bid >= snap.best_ask):
        bid = snap.best_bid
    if bid is not None and ask is not None and bid >= ask:
        return []

    qty = config.paper.quote_size_contracts
    booked = list(resting or [])
    equity = account_value_from_portfolio(portfolio, {snap.slug: snap.mid})
    orders: list[PaperOrder] = []
    for side, price in (("buy", bid), ("sell", ask)):
        if price is None:
            continue
        # Maker-only: never cross or lock the opposite side.
        if side == "buy" and price >= snap.best_ask:
            if pos < 0:
                price = snap.best_bid
            else:
                continue
        if side == "sell" and price <= snap.best_bid:
            if pos > 0:
                price = snap.best_ask
            else:
                continue
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
        if would_breach_risk_limits(portfolio, booked, candidate, equity, config.paper.risk):
            continue
        orders.append(candidate)
        booked.append(candidate)
    return orders


def touch_moved(prev: MarketSnapshot | None, cur: MarketSnapshot, threshold: Decimal) -> bool:
    if prev is None or cur.best_bid is None or cur.best_ask is None:
        return True
    if prev.best_bid is None or prev.best_ask is None:
        return True
    return abs(cur.best_bid - prev.best_bid) >= threshold or abs(cur.best_ask - prev.best_ask) >= threshold


def should_requote(
    prev: MarketSnapshot | None,
    cur: MarketSnapshot,
    last_quote_at: datetime | None,
    now: datetime,
    interval_seconds: float,
    tick: Decimal,
    *,
    touch_ticks: int = 1,
) -> bool:
    if last_quote_at is None:
        return True
    if (now - last_quote_at).total_seconds() >= interval_seconds:
        return True
    return touch_moved(prev, cur, tick * Decimal(max(1, touch_ticks)))
