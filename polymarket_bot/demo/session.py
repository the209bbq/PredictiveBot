"""Opt-in Kalshi DEMO session: quote, re-quote, cancel, then verify flat.

Talks only to a Kalshi demo host. Production trading stays disabled.
Secrets come from environment variables. No transfer endpoints are called.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from polymarket_bot.config import AppConfig
from polymarket_bot.exchanges.kalshi import KalshiClient
from polymarket_bot.guard import DemoOrderError
from polymarket_bot.logging_utils import DecisionLogger, json_default
from polymarket_bot.market_data import MarketSnapshot, as_decimal
from polymarket_bot.market_data.errors import BookFetchError
from polymarket_bot.account_risk import (
    AccountRiskError,
    account_risk_fraction,
    format_risk_pct,
    total_at_risk,
)
from polymarket_bot.market_risk import (
    PriceHistory,
    attach_market_risk,
    format_score,
    is_live_in_game,
    market_over_risk_threshold,
    score_payload,
)
from polymarket_bot.paper import maker
from polymarket_bot.paper.portfolio import PaperOrder, Portfolio
from polymarket_bot import favorites
from polymarket_bot.kalshi_orders import format_v2_price, v2_from_paper_order, v2_would_cross
from polymarket_bot.daily_limits import refresh_daily_limits
from polymarket_bot.paper.risk import (
    past_resolution_cutoff,
    snapshot_account_risk,
    would_breach_account_risk,
    would_breach_risk_limits,
)
from polymarket_bot.pnl import append_pnl
from polymarket_bot.kalshi_account import (
    cash_dollars,
    fill_qty as fill_qty_field,
    mark_price,
    marked_equity,
    portfolio_value_dollars,
    position_avg_price,
    position_map,
    position_qty,
    position_rows,
)
from polymarket_bot.scanner import last_scan_summary, scan_maker_universe
from polymarket_bot.series_filter import maker_min_hours, maker_universe_ok, quote_mode
from polymarket_bot.trading import TradingLock, TradingPaused, trading_is_on


def _money(value: Any) -> str:
    if isinstance(value, Decimal):
        return f"${value.quantize(Decimal('0.01'))}"
    try:
        return f"${Decimal(str(value)).quantize(Decimal('0.01'))}"
    except Exception:
        return str(value)


def _as_decimal(value: Any) -> Decimal | None:
    try:
        parsed = as_decimal(value)
        return parsed
    except (InvalidOperation, ValueError, TypeError):
        return None


def _balance_available(payload: dict[str, Any]) -> Decimal | None:
    return cash_dollars(payload)


def _position_rows(payload: dict[str, Any] | list) -> list[dict[str, Any]]:
    return position_rows(payload)


def _account_value_from_exchange(
    balance: dict[str, Any],
    positions: dict[str, Any] | list,
    mids: dict[str, Decimal | None],
) -> Decimal:
    cash = cash_dollars(balance) or Decimal("0")
    pos = position_map(positions)
    pv = portfolio_value_dollars(balance)
    if pos:
        return marked_equity(cash, pos, mids, portfolio_value=pv)
    if pv is not None:
        return cash + pv
    return cash


def _orders_from_resting(rows: list[dict[str, Any]]) -> list[tuple[str, Decimal, Decimal]]:
    out: list[tuple[str, Decimal, Decimal]] = []
    for row in rows:
        side = str(row.get("side") or row.get("action") or "")
        price = _as_decimal(row.get("yes_price_dollars") or row.get("price") or row.get("yes_price"))
        qty = _as_decimal(row.get("remaining_count") or row.get("count") or row.get("quantity"))
        if not side or price is None or qty is None:
            continue
        out.append((side, price, qty))
    return out


def _position_avg_price(row: dict[str, Any], qty: Decimal) -> Decimal | None:
    return position_avg_price(row, qty)


def _sync_portfolio(port: Portfolio, positions: dict[str, Any] | list) -> None:
    """Mirror every exchange position (including leftovers from earlier sessions)."""
    seen: set[str] = set()
    for row in _position_rows(positions):
        ticker = str(row.get("ticker") or row.get("market_ticker") or "")
        if not ticker:
            continue
        qty = position_qty(row)
        if qty is None:
            continue
        pos = port.position(ticker)
        pos.qty = qty
        avg = position_avg_price(row, qty)
        if avg is not None:
            pos.avg_price = avg
        seen.add(ticker)
    for slug, pos in list(port.positions.items()):
        if slug not in seen:
            pos.qty = Decimal("0")


def _order_id(payload: dict[str, Any]) -> str | None:
    order = payload.get("order") if isinstance(payload.get("order"), dict) else payload
    oid = order.get("order_id") or order.get("id")
    return str(oid) if oid else None


def _refresh_snapshot(
    client: KalshiClient,
    snap: MarketSnapshot,
    logger: DecisionLogger,
    *,
    now: datetime | None = None,
) -> MarketSnapshot:
    now = now or datetime.now(timezone.utc)
    try:
        book = client.book(snap.slug)
    except (BookFetchError, Exception) as exc:
        logger.log("book_error", market=snap.slug, error=str(exc), stale=True, live=False)
        snap.stale = True
        snap.book_fetched = False
        return snap
    market = snap.raw.get("market") or {"ticker": snap.slug, "slug": snap.slug, "question": snap.question}
    refreshed = client.snapshot(market, book, now=now)
    try:
        last = client.last_trade(snap.slug)
        if last is not None:
            refreshed.last_trade = last
    except (BookFetchError, Exception) as exc:
        logger.log("last_trade_error", market=snap.slug, error=str(exc), live=False)
    refreshed.stale = False
    refreshed.book_fetched = True
    return refreshed


def _position_mids(
    client: KalshiClient,
    port: Portfolio,
    snaps: MarketSnapshot | dict[str, MarketSnapshot],
) -> dict[str, Decimal | None]:
    """Mark every open position at mid (or conservative book / last trade)."""
    if isinstance(snaps, MarketSnapshot):
        snap_map = {snaps.slug: snaps}
    else:
        snap_map = dict(snaps)
    mids: dict[str, Decimal | None] = {}
    for slug, snap in snap_map.items():
        mids[slug] = mark_price(snap, port.position(slug).qty, snap.last_trade)
    getter = getattr(client, "last_trade", None)
    book_fn = getattr(client, "book", None)
    snap_fn = getattr(client, "snapshot", None)
    for slug, pos in port.positions.items():
        if pos.qty == 0 or slug in mids:
            continue
        other = None
        if callable(book_fn) and callable(snap_fn):
            try:
                book = book_fn(slug)
                other = snap_fn({"ticker": slug, "slug": slug}, book, now=datetime.now(timezone.utc))
            except Exception:
                other = None
        last = None
        if callable(getter):
            try:
                last = getter(slug)
            except Exception:
                last = None
        fallback = last if last is not None else (pos.avg_price or None)
        mids[slug] = mark_price(other, pos.qty, fallback)
    return mids


def _write_demo_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, default=json_default, indent=2) + "\n")
    tmp.replace(path)
    write_current_session_pointer(path)


@dataclass
class LiveQuote:
    ticker: str
    order: PaperOrder
    book_side: str
    yes_price: Decimal
    qty: Decimal
    order_id: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.ticker, self.book_side)


def quote_target_unchanged(
    existing: LiveQuote,
    desired: PaperOrder,
    book_side: str,
    yes_price: Decimal,
) -> bool:
    """True when cancel/replace would only lose queue at the same price and size."""
    return (
        existing.book_side == book_side
        and format_v2_price(existing.yes_price) == format_v2_price(yes_price)
        and int(existing.qty) == int(desired.qty)
        and existing.order.side == desired.side
        and str(existing.order.contract_side or "") == str(desired.contract_side or "")
    )


def current_session_pointer_path() -> Path:
    root = os.environ.get("PMBOT_STATE_DIR")
    if root:
        return Path(root) / "current"
    return Path("logs/current")


def write_current_session_pointer(state_path: Path) -> None:
    pointer = current_session_pointer_path()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    resolved = Path(state_path)
    try:
        resolved = resolved.resolve()
    except OSError:
        pass
    pointer.write_text(str(resolved) + "\n")


def resolve_demo_state_path(config: AppConfig) -> Path:
    """Follow logs/current when present, else the config demo_state_path."""
    pointer = current_session_pointer_path()
    if pointer.exists():
        raw = pointer.read_text().strip()
        if raw:
            target = Path(raw)
            if target.exists():
                return target
    return config.logging.demo_state_path


def merge_watched_markets(
    kept_current: dict[str, MarketSnapshot],
    scanned: list[MarketSnapshot],
    max_markets: int,
    pinned: str | None = None,
) -> dict[str, MarketSnapshot]:
    """Keep still-eligible markets; fill empty slots from the latest scan."""
    cap = max(1, int(max_markets))
    out: dict[str, MarketSnapshot] = {}
    if pinned:
        if pinned in kept_current:
            out[pinned] = kept_current[pinned]
        else:
            for snap in scanned:
                if snap.slug == pinned:
                    out[pinned] = snap
                    break
    for slug, snap in kept_current.items():
        if len(out) >= cap:
            return out
        out.setdefault(slug, snap)
    for snap in scanned:
        if len(out) >= cap:
            return out
        if snap.slug:
            out.setdefault(snap.slug, snap)
    return out


def _official_close_time(snap: MarketSnapshot) -> datetime | None:
    market = (snap.raw or {}).get("market") or {}
    raw = market.get("close_time") or market.get("latest_expiration_time")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _market_past_close(snap: MarketSnapshot, now: datetime | None = None) -> bool:
    """True after the official close (not kickoff / hours_to_resolution)."""
    status = (snap.status or "").lower()
    if any(tok in status for tok in ("closed", "settled", "finalized", "determined", "inactive")):
        return True
    now = now or datetime.now(timezone.utc)
    close = _official_close_time(snap)
    if close is not None:
        return now >= close
    hours = snap.hours_to_resolution
    return hours is not None and hours <= 0


def _resting_payload(quotes: dict[tuple[str, str], LiveQuote]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for quote in quotes.values():
        rows.append(
            {
                "market": quote.ticker,
                "ticker": quote.ticker,
                "side": quote.book_side,
                "price": format_v2_price(quote.yes_price),
                "qty": int(quote.qty),
                "order_id": quote.order_id,
                "contract_side": quote.order.contract_side,
                "fill_tag": getattr(quote.order, "fill_tag", "") or "",
            }
        )
    return rows


def _cancel_live_quote(
    client: KalshiClient,
    quote: LiveQuote,
    logger: DecisionLogger,
) -> None:
    if not quote.order_id:
        return
    canceller = getattr(client, "cancel_demo_order", None)
    if not callable(canceller):
        return
    try:
        canceller(quote.order_id, ticker=quote.ticker, confirm_demo=True)
        logger.log(
            "cancel",
            reason="replace_or_drop",
            market=quote.ticker,
            order_id=quote.order_id,
            scoped=True,
            live=False,
            demo=True,
        )
    except DemoOrderError as exc:
        logger.log("cancel_error", error=str(exc), market=quote.ticker, live=False)


def _cancel_ticker_quotes(
    client: KalshiClient,
    live_quotes: dict[tuple[str, str], LiveQuote],
    ticker: str,
    logger: DecisionLogger,
) -> int:
    n = 0
    for key in [k for k in live_quotes if k[0] == ticker]:
        quote = live_quotes.pop(key)
        _cancel_live_quote(client, quote, logger)
        n += 1
    return n


def _cancel_all_live_quotes(
    client: KalshiClient,
    live_quotes: dict[tuple[str, str], LiveQuote],
    logger: DecisionLogger,
    *,
    reason: str,
) -> int:
    n = len(live_quotes)
    for key in list(live_quotes):
        _cancel_live_quote(client, live_quotes.pop(key), logger)
    try:
        canceller = getattr(client, "cancel_bot_demo_orders", None)
        if callable(canceller):
            canceller(confirm_demo=True)
    except DemoOrderError as exc:
        logger.log("cancel_error", error=str(exc), live=False)
    logger.log("cancel", reason=reason, scoped=True, live=False, demo=True)
    return n


def _reconcile_favorites_fills(
    client: KalshiClient,
    config: AppConfig,
    logger: DecisionLogger,
    now: datetime,
) -> None:
    path = favorites.favorites_fills_path(config)
    try:
        rows = favorites.load_fill_rows(path)
    except Exception:
        return
    if not rows:
        return
    try:
        updated = favorites.reconcile_fills(rows, now=now, client=client)
    except Exception as exc:
        logger.log("favorites_reconcile_error", error=str(exc), live=False, demo=True)
        return
    if updated != rows:
        try:
            favorites.rewrite_fills(path, updated)
            logger.log(
                "favorites_reconcile",
                n=len(updated),
                settled=sum(1 for row in updated if row.get("settled")),
                live=False,
                demo=True,
            )
        except Exception as exc:
            logger.log("favorites_reconcile_error", error=str(exc), live=False, demo=True)


def _pick_market(
    client: KalshiClient,
    config: AppConfig,
    ticker: str | None,
    now: datetime,
) -> MarketSnapshot:
    if ticker:
        listed = client.list_markets(limit=1, active=True, closed=False, series_ticker=None)
        market = {"ticker": ticker, "slug": ticker, "status": "open"}
        for row in listed:
            if str(row.get("ticker") or row.get("slug")) == ticker:
                market = row
                break
        else:
            try:
                payload = client._get(client.data_base_url, f"/markets/{ticker}")
                market = payload.get("market") or market
                market["slug"] = market.get("ticker") or ticker
            except Exception:
                market = {"ticker": ticker, "slug": ticker, "status": "open"}
        book = client.book(ticker)
        snap = client.snapshot(market, book, now=now)
        snap.stale = False
        snap.book_fetched = True
        if favorites.is_favorites_mode(config):
            why = favorites.book_skip_reason(snap, config, now=now)
            if why:
                logging.getLogger("polymarket_bot").info(
                    "favorites_scan pinned_ticker=%s would_skip=%s hours=%s",
                    ticker,
                    why,
                    snap.hours_to_resolution,
                )
        return snap
    picks = scan_maker_universe(client, config, now=now)
    if not picks:
        extra = last_scan_summary() or "no scan summary"
        raise DemoOrderError(
            "No eligible allowlisted Kalshi DEMO market in the current filters. "
            f"{extra} Pin --ticker if you already have one."
        )
    return picks[0]


def assert_demo_order_within_risk(
    client: KalshiClient,
    config: AppConfig,
    *,
    ticker: str,
    side: str,
    price: str,
    count: str,
    confirm_demo: bool,
) -> Decimal:
    """Refuse a one-shot demo order if trading is off or it would breach the cap."""
    on, reason = trading_is_on(config)
    if not on:
        raise TradingPaused(f"Trading is off ({reason}). Run: pmbot trading on")
    balance = client.demo_balance(confirm_demo=confirm_demo)
    positions = {}
    try:
        positions = client.demo_positions(confirm_demo=confirm_demo)
    except DemoOrderError:
        positions = {}
    resting_rows: list[dict[str, Any]] = []
    lister = getattr(client, "list_demo_orders", None)
    if callable(lister):
        try:
            resting_rows = list(lister(status="resting", confirm_demo=confirm_demo) or [])
        except DemoOrderError:
            resting_rows = []
    px = Decimal(str(price))
    qty = Decimal(str(count))
    equity = _account_value_from_exchange(balance, positions, {ticker: px})
    if equity <= 0:
        equity = _balance_available(balance) or config.paper.starting_cash
    pos_map: dict[str, tuple[Decimal, Decimal]] = {}
    for row in _position_rows(positions):
        slug = str(row.get("ticker") or row.get("market_ticker") or "")
        q = position_qty(row)
        if not slug or q is None or q == 0:
            continue
        avg = _position_avg_price(row, q) or px
        pos_map[slug] = (q, avg)
    orders = _orders_from_resting(resting_rows)
    orders.append((side, px, qty))
    frac = account_risk_fraction(total_at_risk(pos_map, orders), equity)
    if frac > config.paper.risk.max_account_risk_pct:
        raise AccountRiskError(
            f"Order rejected: account risk {format_risk_pct(frac)} would exceed "
            f"cap {format_risk_pct(config.paper.risk.max_account_risk_pct)}."
        )
    at_risk = total_at_risk(pos_map, orders)
    if at_risk > config.paper.risk.max_daily_capital_in_use_usd:
        raise AccountRiskError(
            f"Order rejected: capital in use ${at_risk} would exceed "
            f"${config.paper.risk.max_daily_capital_in_use_usd}."
        )
    return frac


def run_demo_session(
    client: KalshiClient,
    config: AppConfig,
    logger: DecisionLogger,
    *,
    confirm_demo: bool,
    ticks: int | None = None,
    ticker: str | None = None,
    sleep: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Quote / re-quote / cancel on Kalshi DEMO, then verify no resting orders."""
    client._assert_demo(confirm_demo)
    now = now or datetime.now(timezone.utc)
    n_ticks = ticks if ticks is not None else config.paper.ticks
    fav_mode = favorites.is_favorites_mode(config)
    fav_settings = favorites.favorites_settings(config) if fav_mode else None
    max_markets = fav_settings.max_markets if fav_settings else 1
    rescan_seconds = (fav_settings.rescan_minutes * 60.0) if fav_settings else None

    watched: dict[str, MarketSnapshot] = {}
    last_scan_at: datetime | None = None
    if fav_mode:
        pinned_snap = _pick_market(client, config, ticker, now) if ticker else None
        if pinned_snap is not None:
            watched[pinned_snap.slug] = pinned_snap
        try:
            scanned = scan_maker_universe(client, config, now=now)
        except Exception as exc:
            logger.log("scan_error", error=str(exc), live=False, demo=True)
            scanned = []
        watched = merge_watched_markets(watched, scanned, max_markets, pinned=ticker)
        last_scan_at = now
        if not watched:
            extra = last_scan_summary() or "no scan summary"
            raise DemoOrderError(
                "No eligible allowlisted Kalshi DEMO market in the current filters. "
                f"{extra} Pin --ticker if you already have one."
            )
    else:
        snap0 = _pick_market(client, config, ticker, now)
        watched[snap0.slug] = snap0

    snap = next(iter(watched.values()))

    start_balance: dict[str, Any] = {}
    try:
        start_balance = client.demo_balance(confirm_demo=True)
    except DemoOrderError as exc:
        logger.log("balance_error", error=str(exc), live=False)

    cash = _balance_available(start_balance) or config.paper.starting_cash
    port = Portfolio("kalshi_demo", cash, cash)
    quotes_placed = 0
    cancels = 0
    fill_snapshots: list[dict[str, Any]] = []
    position_snapshots: list[dict[str, Any]] = []
    leftover_alert: str | None = None
    remaining: list[dict[str, Any]] = []
    last_risk: dict[str, Any] = {}
    last_scores: dict[str, dict[str, Any]] = {}
    last_trading = "on"
    history = PriceHistory()
    live_quotes: dict[tuple[str, str], LiveQuote] = {}
    last_daily = None
    seen_fav_fills: set[str] = set()

    logger.log(
        "demo_start",
        host=client.demo_base_url,
        market=snap.slug,
        markets=list(watched),
        ticks=n_ticks,
        strategy=favorites.STRATEGY if fav_mode else "two_sided_maker",
        live=False,
        demo=True,
    )

    lock = TradingLock(config.trading.lock_path)
    lock.acquire()
    try:
        try:
            start_positions = client.demo_positions(confirm_demo=True)
            _sync_portfolio(port, start_positions)
            position_snapshots.append(start_positions)
        except DemoOrderError as exc:
            logger.log("position_error", error=str(exc), live=False)

        try:
            canceller = getattr(client, "cancel_bot_demo_orders", None)
            if callable(canceller):
                canceller(confirm_demo=True)
                cancels += 1
                logger.log("cancel", reason="startup_clear", scoped=True, live=False, demo=True)
        except DemoOrderError as exc:
            logger.log("cancel_error", error=str(exc), live=False)

        clock = now
        for tick in range(n_ticks):
            now_tick = clock if sleep is False else datetime.now(timezone.utc)
            if fav_mode and (
                last_scan_at is None
                or rescan_seconds is None
                or rescan_seconds <= 0
                or (now_tick - last_scan_at).total_seconds() >= rescan_seconds
            ):
                try:
                    scanned = scan_maker_universe(client, config, now=now_tick)
                except Exception as exc:
                    logger.log("scan_error", error=str(exc), live=False, demo=True)
                    scanned = []
                watched = merge_watched_markets(watched, scanned, max_markets, pinned=ticker)
                last_scan_at = now_tick
                logger.log(
                    "favorites_rescan",
                    markets=list(watched),
                    scan=last_scan_summary(),
                    live=False,
                    demo=True,
                )

            for slug in list(watched):
                refreshed = _refresh_snapshot(client, watched[slug], logger, now=now_tick)
                attach_market_risk(refreshed, history)
                last_scores[refreshed.slug] = score_payload(refreshed)
                watched[slug] = refreshed
                logger.log(
                    "market_risk",
                    market=refreshed.slug,
                    score=format_score(refreshed.risk_score),
                    live_game=bool((refreshed.risk_components or {}).get("live_game")),
                    live=False,
                    demo=True,
                )
            if watched:
                snap = next(iter(watched.values()))

            trading_on, trading_reason = trading_is_on(config)
            last_trading = "on" if trading_on else "off"

            try:
                pos_payload = client.demo_positions(confirm_demo=True)
                _sync_portfolio(port, pos_payload)
                position_snapshots.append(pos_payload)
            except DemoOrderError as exc:
                logger.log("position_error", error=str(exc), live=False)

            try:
                bal_now = client.demo_balance(confirm_demo=True)
                avail_now = _balance_available(bal_now)
                if avail_now is not None:
                    port.cash = avail_now
            except DemoOrderError:
                bal_now = {}

            mids_now = _position_mids(client, port, watched)
            pos_map_now = position_map(position_snapshots[-1] if position_snapshots else {})
            if pos_map_now:
                equity_now = marked_equity(
                    port.cash,
                    pos_map_now,
                    mids_now,
                    portfolio_value=portfolio_value_dollars(bal_now),
                )
            else:
                equity_now = port.equity(mids_now)
            resting_orders = [q.order for q in live_quotes.values()]
            at_risk_now, _ = snapshot_account_risk(port, resting_orders, equity_now)
            last_daily = refresh_daily_limits(
                config,
                equity=equity_now,
                capital_in_use=at_risk_now,
                now=now_tick,
            )
            if last_daily.just_triggered:
                logger.log(
                    "ALERT",
                    reason="daily_loss_limit",
                    alert=True,
                    daily_pnl=last_daily.day_pnl,
                    limit=last_daily.loss_limit,
                    live=False,
                    demo=True,
                )
            if last_daily.loss_halted:
                trading_on = False
                trading_reason = "daily_loss_limit"

            halt_book = (not trading_on) or bool(last_daily and last_daily.loss_halted)
            if halt_book and live_quotes:
                cancels += _cancel_all_live_quotes(
                    client,
                    live_quotes,
                    logger,
                    reason="trading_off" if not trading_on else "daily_loss_limit",
                )

            placed_this_tick: list[PaperOrder] = []
            drop: list[str] = []
            for slug, market_snap in list(watched.items()):
                skip_reason = None
                mode = quote_mode(market_snap, config, now_tick)
                if halt_book:
                    skip_reason = "trading_off"
                elif _market_past_close(market_snap, now_tick):
                    skip_reason = "past_close"
                    drop.append(slug)
                elif market_snap.stale or not market_snap.book_fetched:
                    skip_reason = "stale_book"
                elif fav_mode:
                    if not favorites.favorites_universe_ok(market_snap, config, now=now_tick):
                        skip_reason = favorites.skip_reason(market_snap, config, now=now_tick) or "series_filter"
                        drop.append(slug)
                    elif is_live_in_game(market_snap):
                        skip_reason = "live_in_game"
                    elif market_over_risk_threshold(market_snap, config.paper.risk.max_market_risk_score):
                        skip_reason = "market_risk_score"
                elif mode == "halt" or not maker_universe_ok(market_snap, config, now=now_tick):
                    skip_reason = "series_filter"
                elif is_live_in_game(market_snap):
                    skip_reason = "live_in_game"
                elif market_over_risk_threshold(market_snap, config.paper.risk.max_market_risk_score):
                    skip_reason = "market_risk_score"
                elif past_resolution_cutoff(market_snap, maker_min_hours(market_snap, config)):
                    skip_reason = "resolution_cutoff"

                if skip_reason:
                    if skip_reason != "trading_off" or slug == next(iter(watched), None):
                        logger.log(
                            "skip_quote" if skip_reason != "trading_off" else "trading_paused",
                            reason=trading_reason if skip_reason == "trading_off" else skip_reason,
                            market=slug,
                            score=format_score(market_snap.risk_score),
                            live=False,
                            demo=True,
                        )
                    n_cancel = _cancel_ticker_quotes(client, live_quotes, slug, logger)
                    cancels += n_cancel
                    continue

                if fav_mode:
                    desired = favorites.desired_quotes(
                        market_snap,
                        port,
                        config,
                        f"d{tick}",
                        resting=[q.order for q in live_quotes.values() if q.ticker != slug],
                        now=now_tick,
                    )
                else:
                    desired = maker.desired_quotes(market_snap, port, config, f"d{tick}", now=now_tick)

                desired_keys: set[tuple[str, str]] = set()
                for order in desired:
                    side, yes_price = v2_from_paper_order(order)
                    key = (market_snap.slug, side)
                    desired_keys.add(key)
                    if v2_would_cross(side, yes_price, market_snap.best_bid, market_snap.best_ask):
                        logger.log(
                            "skip_quote",
                            reason="would_cross",
                            market=market_snap.slug,
                            side=side,
                            price=yes_price,
                            contract_side=getattr(order, "contract_side", None),
                            live=False,
                            demo=True,
                        )
                        existing = live_quotes.get(key)
                        if existing:
                            _cancel_live_quote(client, live_quotes.pop(key), logger)
                            cancels += 1
                        continue
                    existing = live_quotes.get(key)
                    if existing and quote_target_unchanged(existing, order, side, yes_price):
                        continue
                    if existing:
                        _cancel_live_quote(client, live_quotes.pop(key), logger)
                        cancels += 1
                    bal = {}
                    try:
                        bal = client.demo_balance(confirm_demo=True)
                    except DemoOrderError:
                        bal = start_balance
                    equity = _account_value_from_exchange(
                        bal,
                        position_snapshots[-1] if position_snapshots else {},
                        {s: watched[s].mid for s in watched},
                    )
                    if equity <= 0:
                        equity = port.cash
                    book_resting = [
                        q.order for k, q in live_quotes.items() if k != key
                    ] + placed_this_tick
                    risk_reason = would_breach_risk_limits(
                        port,
                        book_resting,
                        order,
                        equity,
                        config.paper.risk,
                    )
                    if risk_reason:
                        logger.log(
                            "skip_quote",
                            reason=risk_reason,
                            market=market_snap.slug,
                            side=side,
                            live=False,
                            demo=True,
                        )
                        continue
                    try:
                        result = client.place_demo_order(
                            ticker=market_snap.slug,
                            side=side,
                            price=format_v2_price(yes_price),
                            count=str(int(order.qty)),
                            confirm_demo=True,
                            post_only=True,
                        )
                        oid = _order_id(result)
                        live_quotes[key] = LiveQuote(
                            ticker=market_snap.slug,
                            order=order,
                            book_side=side,
                            yes_price=yes_price,
                            qty=order.qty,
                            order_id=oid,
                        )
                        quotes_placed += 1
                        placed_this_tick.append(order)
                        logger.log(
                            "quote",
                            strategy=favorites.STRATEGY if fav_mode else "maker",
                            market=order.market,
                            side=side,
                            price=order.price,
                            qty=order.qty,
                            fill_tag=getattr(order, "fill_tag", "") or "",
                            order_id=oid,
                            live=False,
                            demo=True,
                        )
                    except DemoOrderError as exc:
                        logger.log(
                            "quote_error",
                            error=str(exc),
                            market=market_snap.slug,
                            side=side,
                            live=False,
                        )

                for key in [k for k in live_quotes if k[0] == slug and k not in desired_keys]:
                    _cancel_live_quote(client, live_quotes.pop(key), logger)
                    cancels += 1

            for slug in drop:
                watched.pop(slug, None)
                n_cancel = _cancel_ticker_quotes(client, live_quotes, slug, logger)
                cancels += n_cancel
            if watched:
                snap = next(iter(watched.values()))

            try:
                fills = client.demo_fills(confirm_demo=True)
                fill_snapshots = fills
                logger.log("fills_snapshot", count=len(fills), live=False, demo=True)
                if fav_mode:
                    settings = favorites.favorites_settings(config)
                    for row in fills:
                        ticker_fill = str(
                            (row or {}).get("ticker") or (row or {}).get("market_ticker") or ""
                        )
                        fill_snap = watched.get(ticker_fill) or snap
                        logged = favorites.fill_row_from_exchange(
                            row if isinstance(row, dict) else {},
                            snap=fill_snap,
                            placed_at=now_tick,
                            settings=settings,
                        )
                        if favorites.log_favorites_fill(config, logged, seen_fav_fills):
                            logger.log(
                                "favorites_fill",
                                ticker=logged.get("ticker"),
                                side=logged.get("side"),
                                price=logged.get("price"),
                                fill_tag=logged.get("fill_tag"),
                                live=False,
                                demo=True,
                            )
                    _reconcile_favorites_fills(client, config, logger, now_tick)
            except DemoOrderError as exc:
                logger.log("fills_error", error=str(exc), live=False)

            try:
                bal = client.demo_balance(confirm_demo=True)
                avail = _balance_available(bal)
                if avail is not None:
                    port.cash = avail
                mids = _position_mids(client, port, watched)
                equity = _account_value_from_exchange(
                    bal, position_snapshots[-1] if position_snapshots else {}, mids
                )
                if equity <= 0:
                    equity = port.cash
                at_risk, frac = snapshot_account_risk(
                    port, [q.order for q in live_quotes.values()], equity
                )
                last_risk = {
                    "at_risk": at_risk,
                    "equity": equity,
                    "pct": frac,
                    "pct_display": format_risk_pct(frac),
                    "cap": config.paper.risk.max_account_risk_pct,
                    "cap_display": format_risk_pct(config.paper.risk.max_account_risk_pct),
                }
                logger.log(
                    "account_risk",
                    pct=last_risk["pct_display"],
                    at_risk=at_risk,
                    equity=equity,
                    cap=last_risk["cap_display"],
                    live=False,
                    demo=True,
                )
                port.record_equity(mids)
                try:
                    append_pnl(
                        config.dashboard.pnl_path,
                        {
                            "live": False,
                            "demo": True,
                            "starting_cash": cash,
                            "ending_cash": port.cash,
                            "maker": port.to_dict(mids),
                            "account_risk": last_risk,
                        },
                    )
                except Exception:
                    logger.log("pnl_error", error="failed to persist pnl history", live=False, demo=True)
            except DemoOrderError as exc:
                logger.log("balance_error", error=str(exc), live=False)

            try:
                _write_demo_state(
                    config.logging.demo_state_path,
                    {
                        "source": client.source_name,
                        "venue": "kalshi",
                        "demo": True,
                        "live": False,
                        "running": True,
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "tick": tick + 1,
                        "ticks": n_ticks,
                        "host": client.demo_base_url,
                        "markets": list(watched),
                        "quotes_placed": quotes_placed,
                        "cancels": cancels,
                        "fill_count": len(fill_snapshots),
                        "fills": fill_snapshots[-50:],
                        "positions": position_snapshots[-1] if position_snapshots else {},
                        "starting_cash": cash,
                        "ending_cash": port.cash,
                        "maker": port.to_dict({s: watched[s].mid for s in watched} or {snap.slug: snap.mid}),
                        "trading": last_trading,
                        "account_risk": last_risk,
                        "account_risk_cap": format_risk_pct(config.paper.risk.max_account_risk_pct),
                        "market_risk_cap": format_score(config.paper.risk.max_market_risk_score),
                        "market_risk": last_scores,
                        "daily_limits": last_daily.as_dict() if last_daily else {},
                        "daily_loss_limit_hit": bool(last_daily and last_daily.loss_halted),
                        "strategy": favorites.STRATEGY if fav_mode else "two_sided_maker",
                        "resting_orders": _resting_payload(live_quotes),
                        "scan": last_scan_summary(),
                    },
                )
            except Exception:
                logger.log("demo_state_error", error="failed to persist live demo state", live=False, demo=True)

            if tick < n_ticks - 1:
                if sleep:
                    time.sleep(config.paper.poll_interval_seconds)
                    clock = datetime.now(timezone.utc)
                else:
                    clock = now_tick + timedelta(seconds=float(config.paper.poll_interval_seconds))
    finally:
        lock.release()
        try:
            try:
                remaining = client.shutdown_demo_orders(confirm_demo=True, emergency_all=False)
            except TypeError:
                remaining = client.shutdown_demo_orders(confirm_demo=True)
        except DemoOrderError as exc:
            leftover_alert = str(exc)
            logger.log(
                "ALERT_RESTING_ORDERS",
                alert=True,
                error=leftover_alert,
                live=False,
                demo=True,
            )
            remaining = []

    end_balance: dict[str, Any] = {}
    try:
        end_balance = client.demo_balance(confirm_demo=True)
    except DemoOrderError:
        pass
    try:
        end_positions = client.demo_positions(confirm_demo=True)
        position_snapshots.append(end_positions)
    except DemoOrderError:
        end_positions = position_snapshots[-1] if position_snapshots else {}

    end_cash = _balance_available(end_balance) or port.cash
    end_mids = {s: watched[s].mid for s in watched} if watched else {snap.slug: snap.mid}
    state = {
        "source": client.source_name,
        "venue": "kalshi",
        "demo": True,
        "live": False,
        "dry_run": False,
        "host": client.demo_base_url,
        "ticks": n_ticks,
        "markets": list(watched) or [snap.slug],
        "quotes_placed": quotes_placed,
        "cancels": cancels,
        "fill_count": len(fill_snapshots),
        "fills": fill_snapshots,
        "positions": end_positions,
        "starting_balance": start_balance,
        "ending_balance": end_balance,
        "starting_cash": cash,
        "ending_cash": end_cash,
        "resting_leftover": remaining,
        "resting_orders": _resting_payload(live_quotes),
        "resting_alert": leftover_alert,
        "maker": port.to_dict(end_mids),
        "trading": last_trading,
        "account_risk": last_risk,
        "account_risk_cap": format_risk_pct(config.paper.risk.max_account_risk_pct),
        "market_risk_cap": format_score(config.paper.risk.max_market_risk_score),
        "market_risk": last_scores,
        "cancels_scoped": True,
        "daily_limits": last_daily.as_dict() if last_daily else {},
        "daily_loss_limit_hit": bool(last_daily and last_daily.loss_halted),
        "strategy": favorites.STRATEGY if favorites.is_favorites_mode(config) else "two_sided_maker",
        "scan": last_scan_summary(),
    }
    logger.log(
        "demo_end",
        quotes_placed=quotes_placed,
        fills=len(fill_snapshots),
        leftover=bool(leftover_alert or remaining),
        live=False,
        demo=True,
    )
    try:
        append_pnl(config.dashboard.pnl_path, state)
    except Exception:
        logger.log("pnl_error", error="failed to persist pnl history", live=False, demo=True)
    return state


def format_demo_report(state: dict[str, Any]) -> str:
    leftover = state.get("resting_alert") or (
        f"ALERT: {len(state.get('resting_leftover') or [])} resting DEMO orders remain"
        if state.get("resting_leftover")
        else "none"
    )
    positions = state.get("positions") or {}
    pos_rows = _position_rows(positions)
    lines = [
        "Kalshi DEMO session report",
        "DEMO ONLY — production trading disabled; no fund transfers",
        "",
        f"Host: {state.get('host')}",
        f"Strategy: {state.get('strategy') or 'two_sided_maker'}",
        f"Data source: {state.get('source')}",
        f"Ticks: {state.get('ticks')}    Market: {', '.join(state.get('markets') or []) or '(none)'}",
        "",
        f"Quotes placed: {state.get('quotes_placed', 0)}",
        f"Bot-scoped cancel rounds: {state.get('cancels', 0)}",
        f"Fills reported by demo API: {state.get('fill_count', 0)}",
        f"Starting balance/cash: {_money(state.get('starting_cash'))}",
        f"Ending balance/cash: {_money(state.get('ending_cash'))}",
        f"Trading: {state.get('trading') or 'on'}",
        (
            f"Account risk: {(state.get('account_risk') or {}).get('pct_display', 'n/a')} "
            f"(cap {state.get('account_risk_cap') or 'n/a'})"
        ),
        f"Market risk cap: {state.get('market_risk_cap') or '40.0%'}",
        (
            f"Daily capital in use: {_money((state.get('daily_limits') or {}).get('daily_capital_in_use_usd'))} / "
            f"{_money((state.get('daily_limits') or {}).get('max_daily_capital_in_use_usd'))}"
        ),
        (
            "Daily P&L (PT): "
            + (
                "daily loss limit hit"
                if (state.get("daily_limits") or {}).get("daily_loss_limit_hit")
                or state.get("daily_loss_limit_hit")
                else _money((state.get("daily_limits") or {}).get("daily_pnl_usd"))
            )
            + f" / limit -{_money((state.get('daily_limits') or {}).get('max_daily_loss_usd'))}"
        ),
        "",
        "Market risk scores",
    ]
    scores = state.get("market_risk") or {}
    if scores:
        for slug, payload in scores.items():
            display = (payload or {}).get("score_display") or (payload or {}).get("score") or "-"
            comps = (payload or {}).get("components") or {}
            live = " live-game" if comps.get("live_game") else ""
            lines.append(f"  {slug}: {display}{live}")
    else:
        lines.append("  (none)")
    lines.extend(["", "Positions"])
    if pos_rows:
        for row in pos_rows:
            ticker = row.get("ticker") or row.get("market_ticker") or "?"
            qty = position_qty(row) or row.get("position") or row.get("quantity") or row.get("qty")
            avg = position_avg_price(row) or row.get("average_price") or row.get("avg_price") or row.get("avg_px")
            extra = f" avg={avg}" if avg is not None else ""
            lines.append(f"  {ticker}: qty={qty}{extra}")
    else:
        maker_pos = ((state.get("maker") or {}).get("positions") or {})
        if maker_pos:
            for ticker, pos in maker_pos.items():
                lines.append(f"  {ticker}: qty={pos.get('qty')} avg={pos.get('avg_price')}")
        else:
            lines.append("  (flat or none reported)")
    fills = state.get("fills") or []
    lines.append("")
    lines.append("Fills")
    if fills:
        for fill in fills[:20]:
            lines.append(
                f"  {fill.get('ticker') or fill.get('market_ticker') or '?'} "
                f"{fill.get('side') or ''} {fill_qty_field(fill) or fill.get('count') or fill.get('quantity') or ''} "
                f"@ {fill.get('yes_price_dollars') or fill.get('price') or ''}"
            )
        if len(fills) > 20:
            lines.append(f"  … {len(fills) - 20} more")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append(f"Resting leftover after shutdown: {leftover}")
    if state.get("resting_alert") or state.get("resting_leftover"):
        lines.append("ALERT: resting Kalshi DEMO orders remain. Cancel them manually on demo.")
    lines.append("")
    lines.append("This session never calls production hosts or transfer endpoints.")
    return "\n".join(lines) + "\n"
