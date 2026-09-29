"""Maker-only weather favorites: post-only bids on the favorite side.

Study (2,549 settled Kalshi markets, last 12 months): weather 80c+ at 6h
was 1 loss in 425; YES-side favorites lost ~5.7c. This mode buys NO when
YES <= 1-min_price (default 0.20), inside a 15m–6h window, on KXHIGH*/KXRAIN*
only. KXHIGH settles on the official NWS station high, not a Weather Company
forecast page (those terms ban bots; station traps / revisions are a known
risk). No WH / Truth Social / admin feeds. DEMO default; production trading
stays disabled.

The 24h `kxhigh_resolution_day_enabled=false` guard stays for two-sided
maker. This mode has its own `late_entry_enabled` flag.
"""

from __future__ import annotations

import csv
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from polymarket_bot.config import AppConfig
from polymarket_bot.fees import MIN_PRICE, MAX_PRICE, kalshi_maker_fee
from polymarket_bot.logging_utils import json_default
from polymarket_bot.market_data import MarketSnapshot, as_decimal
from polymarket_bot.paper.maker import clamp_price
from polymarket_bot.paper.portfolio import PaperOrder, Portfolio
from polymarket_bot.market_risk import (
    attach_market_risk,
    is_live_in_game,
    market_over_risk_threshold,
)
from polymarket_bot.paper.risk import (
    account_value_from_portfolio,
    would_breach_position,
    would_breach_risk_limits,
)
from polymarket_bot.series_filter import (
    _has_maker_fees,
    _is_disabled,
    _disabled_list,
    event_blackout,
    series_ticker,
)

ZERO = Decimal("0")
ONE = Decimal("1")
TICK = Decimal("0.01")
STRATEGY = "favorites_maker"
FILL_TAG = "favorites_late"

DEFAULT_ALLOW = ("KXHIGH", "KXRAIN")
# Enforced in code even if someone adds these to the favorites allowlist.
EXCLUDE_PREFIXES = (
    "KXPAYROLLS",
    "KXU3",
    "KXCPI",
    "CPI",
    "KXFED",
    "KXJOBLESS",
    "KXINX",
    "KXNASDAQ",
    "KXEURUSD",
    "KXEUR",
    "KXFXY",
    "KXTRUTHSOCIAL",
)
EXCLUDE_CATEGORIES = ("economics", "financials", "sports")
EXCLUDE_TOKENS = (
    "payroll",
    "jobless",
    "unemployment",
    "cpi",
    "s&p",
    "spx",
    "nasdaq",
    "forex",
    "fx ",
)

def _as_side(value: Any) -> str:
    """YAML `no`/`yes` are booleans; keep them as contract sides."""
    if value is False or str(value).lower() in {"false", "no"}:
        return "no"
    if value is True or str(value).lower() in {"true", "yes"}:
        return "yes"
    return str(value).lower()


DEFAULTS = {
    "min_price": Decimal("0.80"),
    "max_price": Decimal("0.97"),
    "allow_yes": False,
    "sides": ("no",),
    "max_hours_to_close": 6.0,
    "min_hours_to_close": 0.25,
    "quote_size_min": Decimal("1"),
    "quote_size_max": Decimal("3"),
    "max_position_per_market": Decimal("5"),
    "max_depth_fraction": Decimal("0.25"),
    "improve_ticks": 1,
    "exit_if_below": None,
    "allow_maker_fee_series": False,
    "late_entry_enabled": True,
    "fill_tag": FILL_TAG,
    "fills_path": "data/favorites_fills.jsonl",
}


@dataclass(frozen=True)
class FavoritesSettings:
    min_price: Decimal
    max_price: Decimal
    allow_yes: bool
    sides: tuple[str, ...]
    max_hours_to_close: float
    min_hours_to_close: float
    quote_size_min: Decimal
    quote_size_max: Decimal
    max_position_per_market: Decimal
    max_depth_fraction: Decimal
    improve_ticks: int
    exit_if_below: Decimal | None
    allow_maker_fee_series: bool
    late_entry_enabled: bool
    fill_tag: str
    fills_path: Path
    allow: tuple[str, ...]


def is_favorites_mode(config: AppConfig) -> bool:
    extra = (config.extra.get("paper") or {}) if isinstance(config.extra, dict) else {}
    name = str(extra.get("strategy") or "two_sided_maker").lower().replace("-", "_")
    return name in {"favorites_maker", "favorites"}


def favorites_settings(config: AppConfig) -> FavoritesSettings:
    extra = (config.extra.get("paper") or {}) if isinstance(config.extra, dict) else {}
    raw = extra.get("favorites") if isinstance(extra.get("favorites"), dict) else {}
    sides_raw = raw.get("sides") or DEFAULTS["sides"]
    sides = tuple(_as_side(s) for s in sides_raw)
    sides = tuple(s for s in sides if s in {"yes", "no"}) or ("no",)
    allow_yes = bool(raw.get("allow_yes", DEFAULTS["allow_yes"]))
    if allow_yes and "yes" not in sides:
        sides = sides + ("yes",)
    if not allow_yes:
        sides = tuple(s for s in sides if s != "yes") or ("no",)
    exit_raw = raw.get("exit_if_below", DEFAULTS["exit_if_below"])
    allow = tuple(str(x).upper() for x in (raw.get("allow") or DEFAULT_ALLOW))
    return FavoritesSettings(
        min_price=Decimal(str(raw.get("min_price", DEFAULTS["min_price"]))),
        max_price=Decimal(str(raw.get("max_price", DEFAULTS["max_price"]))),
        allow_yes=allow_yes,
        sides=sides,
        max_hours_to_close=float(raw.get("max_hours_to_close", DEFAULTS["max_hours_to_close"])),
        min_hours_to_close=float(raw.get("min_hours_to_close", DEFAULTS["min_hours_to_close"])),
        quote_size_min=Decimal(str(raw.get("quote_size_min", DEFAULTS["quote_size_min"]))),
        quote_size_max=Decimal(str(raw.get("quote_size_max", DEFAULTS["quote_size_max"]))),
        max_position_per_market=Decimal(
            str(raw.get("max_position_per_market", DEFAULTS["max_position_per_market"]))
        ),
        max_depth_fraction=Decimal(str(raw.get("max_depth_fraction", DEFAULTS["max_depth_fraction"]))),
        improve_ticks=int(raw.get("improve_ticks", DEFAULTS["improve_ticks"])),
        exit_if_below=None if exit_raw in (None, "", False) else Decimal(str(exit_raw)),
        allow_maker_fee_series=bool(raw.get("allow_maker_fee_series", False)),
        late_entry_enabled=bool(raw.get("late_entry_enabled", True)),
        fill_tag=str(raw.get("fill_tag", FILL_TAG)),
        fills_path=_fills_path(str(raw.get("fills_path", DEFAULTS["fills_path"]))),
        allow=allow,
    )


def _fills_path(raw: str) -> Path:
    path = Path(raw)
    root = os.environ.get("PMBOT_STATE_DIR")
    if root:
        return Path(root) / path.name
    return path


def favorites_fills_path(config: AppConfig) -> Path:
    return favorites_settings(config).fills_path


def _blob(snap: MarketSnapshot) -> str:
    market = (snap.raw or {}).get("market") or {}
    return " ".join(
        filter(
            None,
            [
                snap.slug,
                snap.question,
                snap.event_title,
                snap.category,
                series_ticker(snap),
                str(market.get("category") or ""),
            ],
        )
    ).lower()


def hard_excluded(snap: MarketSnapshot) -> bool:
    """Economics / financials / sports / Truth Social — always off in this mode."""
    series = series_ticker(snap)
    slug = (snap.slug or "").upper()
    if any(series.startswith(p) or slug.startswith(p) for p in EXCLUDE_PREFIXES):
        return True
    category = str(snap.category or "").lower()
    if any(tok in category for tok in EXCLUDE_CATEGORIES):
        return True
    blob = _blob(snap)
    if any(tok in blob for tok in EXCLUDE_TOKENS):
        return True
    return False


def _is_allowed(snap: MarketSnapshot, allow: tuple[str, ...]) -> bool:
    series = series_ticker(snap)
    slug = (snap.slug or "").upper()
    return any(series.startswith(p) or slug.startswith(p) for p in allow if p)


def in_entry_window(snap: MarketSnapshot, settings: FavoritesSettings) -> bool:
    hours = snap.hours_to_resolution
    if hours is None or not settings.late_entry_enabled:
        return False
    return settings.min_hours_to_close <= hours <= settings.max_hours_to_close


def favorites_universe_ok(
    snap: MarketSnapshot,
    config: AppConfig,
    *,
    now: datetime | None = None,
) -> bool:
    settings = favorites_settings(config)
    if hard_excluded(snap):
        return False
    if _is_disabled(snap, _disabled_list(config)):
        return False
    if not _is_allowed(snap, settings.allow):
        return False
    if _has_maker_fees(snap) and not settings.allow_maker_fee_series:
        return False
    if event_blackout(snap, config, now or datetime.now(timezone.utc)):
        return False
    if not in_entry_window(snap, settings):
        return False
    return True


def favorites_listed_ok(
    market: dict[str, Any],
    config: AppConfig,
    *,
    now: datetime | None = None,
) -> bool:
    from polymarket_bot.exchanges.kalshi import snapshot_from_kalshi

    snap = snapshot_from_kalshi(market, None, now=now)
    settings = favorites_settings(config)
    if hard_excluded(snap):
        return False
    if _is_disabled(snap, _disabled_list(config)):
        return False
    if not _is_allowed(snap, settings.allow):
        return False
    if _has_maker_fees(snap) and not settings.allow_maker_fee_series:
        return False
    if event_blackout(snap, config, now or datetime.now(timezone.utc)):
        return False
    if snap.hours_to_resolution is not None and not in_entry_window(snap, settings):
        return False
    return True


def favorite_side(snap: MarketSnapshot, settings: FavoritesSettings) -> str | None:
    """NO when YES ask implies NO bid >= min_price. YES only if allow_yes."""
    yes_bid, yes_ask = snap.best_bid, snap.best_ask
    no_bid = (ONE - yes_ask) if yes_ask is not None else None
    yes_fav = yes_bid is not None and yes_bid >= settings.min_price
    no_fav = no_bid is not None and no_bid >= settings.min_price
    if no_fav and "no" in settings.sides:
        return "no"
    if yes_fav and settings.allow_yes and "yes" in settings.sides:
        return "yes"
    return None


def side_book(snap: MarketSnapshot, side: str) -> tuple[Decimal | None, Decimal | None]:
    """Best bid/ask on the contract side we would buy."""
    if side == "yes":
        return snap.best_bid, snap.best_ask
    if snap.best_ask is None or snap.best_bid is None:
        return None, None
    return ONE - snap.best_ask, ONE - snap.best_bid


def side_mid(snap: MarketSnapshot, side: str) -> Decimal | None:
    bid, ask = side_book(snap, side)
    if bid is None or ask is None:
        return snap.mid if side == "yes" else (ONE - snap.mid if snap.mid is not None else None)
    return (bid + ask) / 2


def would_cross(price: Decimal, best_ask: Decimal | None) -> bool:
    return best_ask is not None and price >= best_ask


def quote_size(snap: MarketSnapshot, portfolio: Portfolio, settings: FavoritesSettings) -> Decimal:
    pos = abs(portfolio.position(snap.slug).qty)
    room = settings.max_position_per_market - pos
    if room <= 0:
        return ZERO
    depth = snap.bid_depth_contracts or ZERO
    if snap.bids:
        depth = max(depth, snap.bids[0].qty)
    capped = (depth * settings.max_depth_fraction).to_integral_value()
    if capped < 1 and depth > 0:
        capped = Decimal("1")
    size = min(settings.quote_size_max, max(settings.quote_size_min, capped or settings.quote_size_min))
    return min(size, room)


def desired_quotes(
    snap: MarketSnapshot,
    portfolio: Portfolio,
    config: AppConfig,
    order_id_prefix: str,
    resting: list[PaperOrder] | None = None,
    now: datetime | None = None,
) -> list[PaperOrder]:
    settings = favorites_settings(config)
    if snap.stale or not snap.book_fetched:
        return []
    if is_live_in_game(snap):
        return []
    if not favorites_universe_ok(snap, config, now=now):
        return []
    if snap.risk_score is None:
        attach_market_risk(snap)
    if market_over_risk_threshold(snap, config.paper.risk.max_market_risk_score):
        return []
    side = favorite_side(snap, settings)
    if side is None:
        return []
    best_bid, best_ask = side_book(snap, side)
    if best_bid is None or best_ask is None:
        return []
    mark = side_mid(snap, side)
    if settings.exit_if_below is not None and mark is not None and mark < settings.exit_if_below:
        return []
    if best_bid < settings.min_price:
        return []
    tick = snap.tick_size or config.paper.tick_size_fallback or TICK
    improve = tick * Decimal(max(0, settings.improve_ticks))
    raw = min(best_bid + improve, settings.max_price)
    raw = max(raw, settings.min_price)
    price = clamp_price(raw, tick)
    if price is None:
        return []
    if price < settings.min_price or price > settings.max_price:
        return []
    if would_cross(price, best_ask):
        price = clamp_price(min(best_bid, settings.max_price, best_ask - tick), tick)
        if price is None or would_cross(price, best_ask) or price < settings.min_price:
            return []
    qty = quote_size(snap, portfolio, settings)
    if qty <= 0:
        return []
    if would_breach_position(portfolio, snap.slug, "buy", qty, config.paper.risk):
        return []
    if not portfolio.buying_power_ok("buy", price, qty):
        return []
    equity = account_value_from_portfolio(portfolio, {snap.slug: mark or snap.mid})
    order = PaperOrder(
        order_id=f"{order_id_prefix}-{snap.slug}-fav-{side}",
        market=snap.slug,
        side="buy",
        price=price,
        qty=qty,
        strategy=STRATEGY,
        venue=snap.venue,
        fee_type=snap.fee_type,
        fee_multiplier=snap.fee_multiplier,
        contract_side=side,
        fill_tag=settings.fill_tag,
    )
    if would_breach_risk_limits(portfolio, resting or [], order, equity, config.paper.risk):
        return []
    return [order]


def listed_skip_reason(
    market: dict[str, Any],
    config: AppConfig,
    *,
    now: datetime | None = None,
) -> str | None:
    """Why a list row is dropped before a book fetch. None = keep."""
    from polymarket_bot.exchanges.kalshi import snapshot_from_kalshi

    snap = snapshot_from_kalshi(market, None, now=now)
    settings = favorites_settings(config)
    if hard_excluded(snap):
        return "favorites_excluded"
    if _is_disabled(snap, _disabled_list(config)):
        return "disabled"
    if not _is_allowed(snap, settings.allow):
        return "series_filter"
    if _has_maker_fees(snap) and not settings.allow_maker_fee_series:
        return "maker_fee_series"
    if event_blackout(snap, config, now or datetime.now(timezone.utc)):
        return "event_blackout"
    if snap.hours_to_resolution is None:
        return "no_close_time"
    if not in_entry_window(snap, settings):
        return "favorites_window"
    return None


def book_skip_reason(
    snap: MarketSnapshot,
    config: AppConfig,
    *,
    now: datetime | None = None,
    risk_cap=None,
) -> str | None:
    from polymarket_bot.market_risk import market_over_risk_threshold

    if snap.stale or not snap.book_fetched:
        return "stale_book"
    if snap.best_bid is None or snap.best_ask is None:
        return "no_book"
    why = skip_reason(snap, config, now=now)
    if why:
        return why
    cap = risk_cap if risk_cap is not None else config.paper.risk.max_market_risk_score
    if market_over_risk_threshold(snap, cap):
        return "market_risk_score"
    if favorite_side(snap, favorites_settings(config)) is None:
        return "favorites_side"
    return None


def skip_reason(snap: MarketSnapshot, config: AppConfig, *, now: datetime | None = None) -> str | None:
    settings = favorites_settings(config)
    if hard_excluded(snap):
        return "favorites_excluded"
    if not _is_allowed(snap, settings.allow):
        return "series_filter"
    if _has_maker_fees(snap) and not settings.allow_maker_fee_series:
        return "maker_fee_series"
    if not in_entry_window(snap, settings):
        return "favorites_window"
    if event_blackout(snap, config, now or datetime.now(timezone.utc)):
        return "series_filter"
    if favorite_side(snap, settings) is None:
        return "favorites_side"
    return None


def append_fill_row(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(row, default=json_default) + "\n")


def load_fill_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def fill_row_from_order(
    order: PaperOrder,
    *,
    snap: MarketSnapshot,
    filled_qty: Decimal,
    filled_at: datetime,
    placed_at: datetime | None = None,
    settings: FavoritesSettings | None = None,
) -> dict[str, Any]:
    settings = settings or FavoritesSettings(
        min_price=Decimal("0.80"),
        max_price=Decimal("0.97"),
        allow_yes=False,
        sides=("no",),
        max_hours_to_close=6,
        min_hours_to_close=0.25,
        quote_size_min=Decimal("1"),
        quote_size_max=Decimal("3"),
        max_position_per_market=Decimal("5"),
        max_depth_fraction=Decimal("0.25"),
        improve_ticks=1,
        exit_if_below=None,
        allow_maker_fee_series=False,
        late_entry_enabled=True,
        fill_tag=FILL_TAG,
        fills_path=Path("data/favorites_fills.jsonl"),
        allow=DEFAULT_ALLOW,
    )
    side = order.contract_side or "yes"
    bid, ask = side_book(snap, side)
    fee = kalshi_maker_fee(filled_qty, order.price, fee_type=order.fee_type, multiplier=order.fee_multiplier)
    return {
        "ticker": order.market,
        "series": series_ticker(snap),
        "category": snap.category,
        "side": side,
        "price": str(order.price),
        "contracts": str(filled_qty),
        "maker_fee": str(fee),
        "ts_placed": (placed_at or filled_at).isoformat(),
        "ts_filled": filled_at.isoformat(),
        "hours_to_close": snap.hours_to_resolution,
        "best_bid": str(bid) if bid is not None else None,
        "best_ask": str(ask) if ask is not None else None,
        "mid": str(side_mid(snap, side) or ""),
        "mid_1m": None,
        "mid_5m": None,
        "mid_30m": None,
        "settled": False,
        "outcome": None,
        "pnl_after_fees": None,
        "fill_tag": order.fill_tag or settings.fill_tag,
        "strategy": STRATEGY,
    }


def fill_row_from_exchange(
    fill: dict[str, Any],
    *,
    snap: MarketSnapshot | None = None,
    placed_at: datetime | None = None,
    settings: FavoritesSettings | None = None,
) -> dict[str, Any]:
    """Map a Kalshi DEMO fill payload onto the measurement log."""
    from polymarket_bot.kalshi_account import fill_qty as exchange_qty

    side = str(fill.get("side") or fill.get("action") or "no").lower()
    if side in {"bid", "buy"}:
        side = "yes"
    if side in {"ask", "sell"}:
        side = "no"
    if side not in {"yes", "no"}:
        side = "no"
    yes_px = as_decimal(fill.get("yes_price_dollars") or fill.get("price") or fill.get("yes_price"))
    no_px = as_decimal(fill.get("no_price_dollars") or fill.get("no_price"))
    price = no_px if side == "no" and no_px is not None else yes_px
    if price is not None and side == "no" and no_px is None and yes_px is not None:
        price = ONE - yes_px
    qty = exchange_qty(fill) or as_decimal(fill.get("count") or fill.get("quantity")) or ZERO
    fee = kalshi_maker_fee(qty, price or ZERO, fee_type=(snap.fee_type if snap else None))
    filled_at = _parse_ts(fill.get("created_time") or fill.get("ts") or fill.get("timestamp")) or datetime.now(
        timezone.utc
    )
    ticker = str(fill.get("ticker") or fill.get("market_ticker") or (snap.slug if snap else ""))
    bid = ask = mid = None
    hours = None
    category = None
    series = ""
    if snap is not None:
        bid, ask = side_book(snap, side)
        mid = side_mid(snap, side)
        hours = snap.hours_to_resolution
        category = snap.category
        series = series_ticker(snap)
    tag = FILL_TAG if settings is None else settings.fill_tag
    return {
        "ticker": ticker,
        "series": series or str(fill.get("series_ticker") or ticker.split("-", 1)[0]),
        "category": category,
        "side": side,
        "price": str(price or ZERO),
        "contracts": str(qty),
        "maker_fee": str(fee),
        "ts_placed": (placed_at or filled_at).isoformat(),
        "ts_filled": filled_at.isoformat(),
        "hours_to_close": hours,
        "best_bid": str(bid) if bid is not None else None,
        "best_ask": str(ask) if ask is not None else None,
        "mid": str(mid) if mid is not None else "",
        "mid_1m": None,
        "mid_5m": None,
        "mid_30m": None,
        "settled": False,
        "outcome": None,
        "pnl_after_fees": None,
        "fill_tag": fill.get("fill_tag") or tag,
        "strategy": STRATEGY,
        "order_id": str(fill.get("order_id") or fill.get("trade_id") or ""),
    }


def fill_key(row: dict[str, Any]) -> str:
    return "|".join(
        [
            str(row.get("order_id") or ""),
            str(row.get("ticker") or ""),
            str(row.get("ts_filled") or ""),
            str(row.get("side") or ""),
            str(row.get("price") or ""),
            str(row.get("contracts") or ""),
        ]
    )


def log_favorites_fill(config: AppConfig, row: dict[str, Any], seen: set[str] | None = None) -> bool:
    key = fill_key(row)
    if seen is not None:
        if key in seen:
            return False
        seen.add(key)
    append_fill_row(favorites_fills_path(config), row)
    return True


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 0.0)
    p = wins / n
    z2 = z * z
    den = 1 + z2 / n
    center = (p + z2 / (2 * n)) / den
    margin = z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n) / den
    return (max(0.0, center - margin), min(1.0, center + margin))


def _hours_bucket(hours: Any) -> str:
    try:
        h = float(hours)
    except (TypeError, ValueError):
        return "unknown"
    if h < 1:
        return "15-60m"
    if h < 3:
        return "1-3h"
    return "3-6h"


def summarize_fills(rows: list[dict[str, Any]], *, nested: bool = True) -> dict[str, Any]:
    settled = [r for r in rows if r.get("settled") and r.get("outcome") is not None]
    n = len(settled)
    wins = 0
    pnl = Decimal("0")
    prices: list[Decimal] = []
    adverse: list[Decimal] = []
    for row in settled:
        price = as_decimal(row.get("price")) or ZERO
        qty = as_decimal(row.get("contracts")) or ZERO
        fee = as_decimal(row.get("maker_fee")) or ZERO
        prices.append(price)
        won = str(row.get("outcome") or "").lower() == str(row.get("side") or "").lower()
        if won:
            wins += 1
        raw = as_decimal(row.get("pnl_after_fees"))
        if raw is None:
            raw = ((ONE - price) * qty - fee) if won else (-price * qty - fee)
        pnl += raw
        mid0 = as_decimal(row.get("mid"))
        mid_after = as_decimal(row.get("mid_1m")) or as_decimal(row.get("mid_5m"))
        if mid0 is not None and mid_after is not None:
            adverse.append(mid_after - mid0)
    avg_price = (sum(prices, ZERO) / len(prices)) if prices else ZERO
    win_rate = (wins / n) if n else 0.0
    lo, hi = wilson_interval(wins, n)
    total_qty = sum((as_decimal(r.get("contracts")) or ZERO) for r in settled)
    per = (pnl / total_qty) if total_qty else ZERO

    def _split(key_fn) -> dict[str, Any]:
        groups: dict[str, list] = {}
        for row in settled:
            groups.setdefault(key_fn(row), []).append(row)
        out = {}
        for key, items in groups.items():
            out[key] = summarize_fills(items, nested=False) | {"n": len(items)}
        return out

    splits = {}
    if nested:
        splits = {
            "by_series": _split(lambda r: str(r.get("series") or "?")),
            "by_side": _split(lambda r: str(r.get("side") or "?")),
            "by_horizon": _split(lambda r: _hours_bucket(r.get("hours_to_close"))),
        }

    return {
        "n_fills": len(rows),
        "n_settled": n,
        "wins": wins,
        "win_rate": win_rate,
        "win_lo95": lo,
        "win_hi95": hi,
        "avg_price_paid": avg_price,
        "breakeven": avg_price,
        "pnl_after_fees": pnl,
        "pnl_per_contract": per,
        "n_contracts": total_qty,
        "avg_adverse_1m": (sum(adverse, ZERO) / len(adverse)) if adverse else ZERO,
        **splits,
    }


def format_favorites_report(summary: dict[str, Any]) -> str:
    lines = [
        "Favorites strategy report (maker-only, DEMO measurement)",
        "",
        f"N fills: {summary.get('n_fills', 0)}  settled: {summary.get('n_settled', 0)}",
        (
            f"Win rate: {float(summary.get('win_rate') or 0):.1%} "
            f"(Wilson 95% {float(summary.get('win_lo95') or 0):.1%}–{float(summary.get('win_hi95') or 0):.1%})"
        ),
        f"Avg price paid / breakeven: {summary.get('avg_price_paid')}",
        f"P&L after fees: {summary.get('pnl_after_fees')}  per contract: {summary.get('pnl_per_contract')}",
        f"Avg adverse mid move after fill: {summary.get('avg_adverse_1m')}",
        "",
        "By series: " + ", ".join(f"{k} n={v.get('n_settled', v.get('n'))}" for k, v in (summary.get("by_series") or {}).items()),
        "By side: " + ", ".join(f"{k} n={v.get('n_settled', v.get('n'))}" for k, v in (summary.get("by_side") or {}).items()),
        "By horizon: " + ", ".join(f"{k} n={v.get('n_settled', v.get('n'))}" for k, v in (summary.get("by_horizon") or {}).items()),
    ]
    return "\n".join(lines) + "\n"


def write_csv(rows: list[dict[str, Any]], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "ticker",
        "series",
        "category",
        "side",
        "price",
        "contracts",
        "maker_fee",
        "ts_placed",
        "ts_filled",
        "hours_to_close",
        "best_bid",
        "best_ask",
        "mid",
        "mid_1m",
        "mid_5m",
        "mid_30m",
        "settled",
        "outcome",
        "pnl_after_fees",
        "fill_tag",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in fields})
    return path


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def reconcile_fills(
    rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    markets: dict[str, dict[str, Any]] | None = None,
    mids_at: dict[str, dict[str, Decimal]] | None = None,
    client: Any | None = None,
) -> list[dict[str, Any]]:
    """Attach post-fill mids and settlement P&L. Client polling is best-effort."""
    now = now or datetime.now(timezone.utc)
    markets = markets or {}
    mids_at = mids_at or {}
    out = []
    for row in rows:
        updated = dict(row)
        ticker = str(row.get("ticker") or "")
        filled = _parse_ts(row.get("ts_filled"))
        side = str(row.get("side") or "yes")
        price = as_decimal(row.get("price")) or ZERO
        qty = as_decimal(row.get("contracts")) or ZERO
        fee = as_decimal(row.get("maker_fee")) or ZERO
        marks = mids_at.get(ticker) or {}
        if client is not None and ticker:
            getter = getattr(client, "last_trade", None)
            market_fn = getattr(client, "market", None) or getattr(client, "get_market", None)
            if callable(market_fn) and ticker not in markets:
                try:
                    payload = market_fn(ticker)
                    if isinstance(payload, dict):
                        markets[ticker] = payload.get("market") or payload
                except Exception:
                    pass
            if callable(getter) and filled is not None:
                try:
                    last = getter(ticker)
                except Exception:
                    last = None
                if last is not None:
                    elapsed = (now - filled).total_seconds()
                    if elapsed >= 60 and updated.get("mid_1m") in (None, ""):
                        updated["mid_1m"] = str(last)
                    if elapsed >= 300 and updated.get("mid_5m") in (None, ""):
                        updated["mid_5m"] = str(last)
                    if elapsed >= 1800 and updated.get("mid_30m") in (None, ""):
                        updated["mid_30m"] = str(last)
        for label, seconds in (("mid_1m", 60), ("mid_5m", 300), ("mid_30m", 1800)):
            if updated.get(label) not in (None, ""):
                continue
            if label in marks:
                updated[label] = str(marks[label])
            elif filled is not None and (now - filled) >= timedelta(seconds=seconds) and marks.get("now") is not None:
                updated[label] = str(marks["now"])
        market = markets.get(ticker) or {}
        status = str(market.get("status") or market.get("result") or "").lower()
        result = market.get("result") or market.get("settlement_value")
        if result or status in {"settled", "finalized", "determined"}:
            outcome = str(result or market.get("yes_winner") or "").lower()
            if outcome in {"yes", "no"}:
                updated["settled"] = True
                updated["outcome"] = outcome
                won = outcome == side
                updated["pnl_after_fees"] = str(((ONE - price) * qty - fee) if won else (-price * qty - fee))
        out.append(updated)
    return out


def rewrite_fills(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(r, default=json_default) + "\n" for r in rows))
    tmp.replace(path)
