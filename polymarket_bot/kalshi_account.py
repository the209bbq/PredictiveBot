"""Parse Kalshi portfolio payloads (cents vs dollars, position_fp)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from polymarket_bot.market_data import MarketSnapshot, as_decimal

ZERO = Decimal("0")
HUNDRED = Decimal("100")


def _dec(value: Any) -> Decimal | None:
    return as_decimal(value)


def cash_dollars(payload: dict[str, Any] | None) -> Decimal | None:
    """Cash available. Prefer balance_dollars; `balance` is cents."""
    if not isinstance(payload, dict):
        return None
    dollars = _dec(payload.get("balance_dollars"))
    if dollars is not None:
        return dollars
    for key in ("available_balance_dollars", "cash_dollars"):
        parsed = _dec(payload.get(key))
        if parsed is not None:
            return parsed
    cents = _dec(payload.get("balance"))
    if cents is not None:
        return cents / HUNDRED
    for key in ("available_balance", "available", "cash"):
        parsed = _dec(payload.get(key))
        if parsed is not None:
            return parsed / HUNDRED if key != "cash" else parsed
    return None


def portfolio_value_dollars(payload: dict[str, Any] | None) -> Decimal | None:
    """Open-position value. `portfolio_value` is cents."""
    if not isinstance(payload, dict):
        return None
    dollars = _dec(payload.get("portfolio_value_dollars"))
    if dollars is not None:
        return dollars
    cents = _dec(payload.get("portfolio_value"))
    if cents is not None:
        return cents / HUNDRED
    return None


def position_rows(payload: dict[str, Any] | list | None) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("market_positions", "positions", "event_positions"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def position_qty(row: dict[str, Any]) -> Decimal | None:
    return _dec(
        row.get("position_fp")
        if row.get("position_fp") not in (None, "")
        else row.get("position") or row.get("quantity") or row.get("qty")
    )


def position_avg_price(row: dict[str, Any], qty: Decimal | None = None) -> Decimal | None:
    avg = _dec(row.get("average_price") or row.get("avg_price") or row.get("avg_px") or row.get("average_price_dollars"))
    if avg is not None:
        return abs(avg)
    qty = qty if qty is not None else position_qty(row)
    exposure = _dec(row.get("market_exposure_dollars") or row.get("market_exposure") or row.get("exposure"))
    if exposure is not None and qty:
        return abs(exposure / qty)
    return None


def position_map(payload: dict[str, Any] | list | None) -> dict[str, tuple[Decimal, Decimal]]:
    """ticker -> (signed qty, avg price) for capital / exposure."""
    out: dict[str, tuple[Decimal, Decimal]] = {}
    for row in position_rows(payload):
        slug = str(row.get("ticker") or row.get("market_ticker") or "")
        qty = position_qty(row)
        if not slug or qty is None or qty == 0:
            continue
        avg = position_avg_price(row, qty) or ZERO
        out[slug] = (qty, avg)
    return out


def mark_price(snap: MarketSnapshot | None, qty: Decimal, fallback: Decimal | None = None) -> Decimal | None:
    """Current mark: mid, else conservative book side, else fallback."""
    if snap is None:
        return fallback
    if snap.mid is not None:
        return snap.mid
    if qty >= 0 and snap.best_bid is not None:
        return snap.best_bid
    if qty < 0 and snap.best_ask is not None:
        return snap.best_ask
    if snap.best_bid is not None:
        return snap.best_bid
    if snap.best_ask is not None:
        return snap.best_ask
    return snap.last_trade if snap.last_trade is not None else fallback


def marked_equity(
    cash: Decimal,
    positions: dict[str, tuple[Decimal, Decimal]],
    mids: dict[str, Decimal | None],
    *,
    portfolio_value: Decimal | None = None,
) -> Decimal:
    """Cash plus open positions marked at current prices."""
    marked = ZERO
    missing = False
    for slug, (qty, avg) in positions.items():
        if qty == 0:
            continue
        px = mids.get(slug)
        if px is None:
            missing = True
            px = avg
        if px is None:
            continue
        marked += qty * px
    if missing and portfolio_value is not None and not any(mids.values()):
        return cash + portfolio_value
    return cash + marked


def fill_qty(row: dict[str, Any]) -> Decimal | None:
    return _dec(row.get("qty") or row.get("count_fp") or row.get("count") or row.get("quantity") or row.get("remaining_count"))
