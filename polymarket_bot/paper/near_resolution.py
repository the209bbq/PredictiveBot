"""Near-resolution favorites: resting bids on ~94–98¢ contracts."""

from __future__ import annotations

from polymarket_bot.config import AppConfig
from polymarket_bot.market_data import MarketSnapshot
from polymarket_bot.paper.maker import clamp_price
from polymarket_bot.paper.portfolio import PaperOrder, Portfolio
from polymarket_bot.paper.risk import would_breach_position

STRATEGY = "near_resolution"


def desired_quotes(
    snap: MarketSnapshot,
    portfolio: Portfolio,
    config: AppConfig,
    order_id_prefix: str,
) -> list[PaperOrder]:
    near = config.paper.near_resolution
    if not near.enabled:
        return []
    if snap.stale or not snap.book_fetched:
        return []
    if snap.mid is None or snap.best_bid is None or snap.best_ask is None:
        return []
    hours = snap.hours_to_resolution
    if hours is None:
        return []
    if hours > near.max_hours_to_resolution or hours < near.min_hours_to_resolution:
        return []
    if snap.mid < near.min_price or snap.mid > near.max_price:
        return []

    tick = snap.tick_size or config.paper.tick_size_fallback
    # Resting bid just below the current bid so we stay maker.
    price = clamp_price(snap.best_bid, tick)
    if price is None or price >= snap.best_ask:
        return []
    qty = near.quote_size_contracts
    if would_breach_position(portfolio, snap.slug, "buy", qty, config.paper.risk):
        return []
    if not portfolio.buying_power_ok("buy", price, qty):
        return []
    return [
        PaperOrder(
            order_id=f"{order_id_prefix}-{snap.slug}-buy",
            market=snap.slug,
            side="buy",
            price=price,
            qty=qty,
            strategy=STRATEGY,
            venue=snap.venue,
            fee_type=snap.fee_type,
            fee_multiplier=snap.fee_multiplier,
        )
    ]
