"""Opt-in Kalshi DEMO session: quote, re-quote, cancel, then verify flat.

Talks only to a Kalshi demo host. Production trading stays disabled.
Secrets come from environment variables. No transfer endpoints are called.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
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
from polymarket_bot.scanner import scan_maker_universe
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
) -> MarketSnapshot:
    try:
        book = client.book(snap.slug)
    except (BookFetchError, Exception) as exc:
        logger.log("book_error", market=snap.slug, error=str(exc), stale=True, live=False)
        snap.stale = True
        snap.book_fetched = False
        return snap
    market = snap.raw.get("market") or {"ticker": snap.slug, "slug": snap.slug, "question": snap.question}
    refreshed = client.snapshot(market, book, now=datetime.now(timezone.utc))
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
    snap: MarketSnapshot,
) -> dict[str, Decimal | None]:
    """Mark every open position at mid (or conservative book / last trade)."""
    mids: dict[str, Decimal | None] = {
        snap.slug: mark_price(snap, port.position(snap.slug).qty, snap.last_trade)
    }
    getter = getattr(client, "last_trade", None)
    book_fn = getattr(client, "book", None)
    snap_fn = getattr(client, "snapshot", None)
    for slug, pos in port.positions.items():
        if pos.qty == 0 or slug == snap.slug:
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
        return snap
    picks = scan_maker_universe(client, config, now=now)
    if not picks:
        raise DemoOrderError(
            "No eligible allowlisted Kalshi DEMO market (series allow/deny or risk-score gate)."
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
    snap = _pick_market(client, config, ticker, now)

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
    placed_this_tick: list[PaperOrder] = []
    last_quote_at: datetime | None = None
    prev_snap: MarketSnapshot | None = None
    last_daily = None
    seen_fav_fills: set[str] = set()

    logger.log(
        "demo_start",
        host=client.demo_base_url,
        market=snap.slug,
        ticks=n_ticks,
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

        for tick in range(n_ticks):
            snap = _refresh_snapshot(client, snap, logger)
            attach_market_risk(snap, history)
            last_scores[snap.slug] = score_payload(snap)
            logger.log(
                "market_risk",
                market=snap.slug,
                score=format_score(snap.risk_score),
                live_game=bool((snap.risk_components or {}).get("live_game")),
                live=False,
                demo=True,
            )
            trading_on, trading_reason = trading_is_on(config)
            last_trading = "on" if trading_on else "off"
            now_tick = datetime.now(timezone.utc)
            tick_sz = snap.tick_size or config.paper.tick_size_fallback
            requote = (not trading_on) or maker.should_requote(
                prev_snap,
                snap,
                last_quote_at,
                now_tick,
                config.paper.maker.requote_interval_seconds,
                tick_sz,
                touch_ticks=config.paper.maker.requote_on_touch_ticks,
            )
            if requote:
                try:
                    canceller = getattr(client, "cancel_bot_demo_orders", None)
                    if callable(canceller):
                        canceller(confirm_demo=True)
                    else:
                        client.cancel_all_demo_orders(confirm_demo=True)
                    cancels += 1
                    logger.log(
                        "cancel",
                        reason="trading_off" if not trading_on else "requote",
                        market=snap.slug,
                        scoped=True,
                        live=False,
                        demo=True,
                    )
                except DemoOrderError as exc:
                    logger.log("cancel_error", error=str(exc), market=snap.slug, live=False)

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

            mids_now = _position_mids(client, port, snap)
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
            at_risk_now, _ = snapshot_account_risk(port, placed_this_tick, equity_now)
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
                if not requote:
                    try:
                        canceller = getattr(client, "cancel_bot_demo_orders", None)
                        if callable(canceller):
                            canceller(confirm_demo=True)
                        cancels += 1
                    except DemoOrderError as exc:
                        logger.log("cancel_error", error=str(exc), market=snap.slug, live=False)

            skip_reason = None
            fav_mode = favorites.is_favorites_mode(config)
            mode = quote_mode(snap, config, now_tick)
            if mode == "unwind" and not requote and not fav_mode:
                requote = True
            if not trading_on:
                skip_reason = "trading_off"
            elif snap.stale or not snap.book_fetched:
                skip_reason = "stale_book"
            elif fav_mode:
                if not favorites.favorites_universe_ok(snap, config, now=now_tick):
                    skip_reason = favorites.skip_reason(snap, config, now=now_tick) or "series_filter"
                    if not requote:
                        try:
                            canceller = getattr(client, "cancel_bot_demo_orders", None)
                            if callable(canceller):
                                canceller(confirm_demo=True)
                            cancels += 1
                        except DemoOrderError as exc:
                            logger.log("cancel_error", error=str(exc), market=snap.slug, live=False)
                elif is_live_in_game(snap):
                    skip_reason = "live_in_game"
                elif market_over_risk_threshold(snap, config.paper.risk.max_market_risk_score):
                    skip_reason = "market_risk_score"
            elif mode == "halt" or not maker_universe_ok(snap, config, now=now_tick):
                skip_reason = "series_filter"
                if not requote:
                    try:
                        canceller = getattr(client, "cancel_bot_demo_orders", None)
                        if callable(canceller):
                            canceller(confirm_demo=True)
                        cancels += 1
                    except DemoOrderError as exc:
                        logger.log("cancel_error", error=str(exc), market=snap.slug, live=False)
            elif is_live_in_game(snap):
                skip_reason = "live_in_game"
            elif market_over_risk_threshold(snap, config.paper.risk.max_market_risk_score):
                skip_reason = "market_risk_score"
            elif past_resolution_cutoff(snap, maker_min_hours(snap, config)):
                skip_reason = "resolution_cutoff"

            placed_this_tick = []
            if skip_reason:
                logger.log(
                    "skip_quote" if skip_reason != "trading_off" else "trading_paused",
                    reason=trading_reason if skip_reason == "trading_off" else skip_reason,
                    market=snap.slug,
                    score=format_score(snap.risk_score),
                    live=False,
                    demo=True,
                )
            elif requote:
                if fav_mode:
                    desired = favorites.desired_quotes(snap, port, config, f"d{tick}", now=now_tick)
                else:
                    desired = maker.desired_quotes(snap, port, config, f"d{tick}", now=now_tick)
                for order in desired:
                    if fav_mode:
                        side = getattr(order, "contract_side", None) or "no"
                        _bid, best_ask = favorites.side_book(snap, side)
                        if favorites.would_cross(order.price, best_ask):
                            logger.log(
                                "skip_quote",
                                reason="would_cross",
                                market=snap.slug,
                                side=side,
                                price=order.price,
                                live=False,
                                demo=True,
                            )
                            continue
                    else:
                        side = "bid" if order.side == "buy" else "ask"
                    bal = {}
                    try:
                        bal = client.demo_balance(confirm_demo=True)
                    except DemoOrderError:
                        bal = start_balance
                    equity = _account_value_from_exchange(
                        bal, position_snapshots[-1] if position_snapshots else {}, {snap.slug: snap.mid}
                    )
                    if equity <= 0:
                        equity = port.cash
                    risk_reason = would_breach_risk_limits(
                        port,
                        placed_this_tick,
                        order,
                        equity,
                        config.paper.risk,
                    )
                    if risk_reason:
                        logger.log(
                            "skip_quote",
                            reason=risk_reason,
                            market=snap.slug,
                            side=side,
                            live=False,
                            demo=True,
                        )
                        continue
                    try:
                        result = client.place_demo_order(
                            ticker=snap.slug,
                            side=side,
                            price=str(order.price),
                            count=str(int(order.qty)),
                            confirm_demo=True,
                            post_only=True,
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
                            order_id=_order_id(result),
                            live=False,
                            demo=True,
                        )
                    except DemoOrderError as exc:
                        logger.log("quote_error", error=str(exc), market=snap.slug, side=side, live=False)
                last_quote_at = now_tick

            prev_snap = snap
            try:
                fills = client.demo_fills(confirm_demo=True)
                fill_snapshots = fills
                logger.log("fills_snapshot", count=len(fills), live=False, demo=True)
                if fav_mode:
                    settings = favorites.favorites_settings(config)
                    for row in fills:
                        logged = favorites.fill_row_from_exchange(
                            row if isinstance(row, dict) else {},
                            snap=snap,
                            placed_at=last_quote_at,
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
            except DemoOrderError as exc:
                logger.log("fills_error", error=str(exc), live=False)

            try:
                bal = client.demo_balance(confirm_demo=True)
                avail = _balance_available(bal)
                if avail is not None:
                    port.cash = avail
                mids = _position_mids(client, port, snap)
                equity = _account_value_from_exchange(
                    bal, position_snapshots[-1] if position_snapshots else {}, mids
                )
                if equity <= 0:
                    equity = port.cash
                at_risk, frac = snapshot_account_risk(port, placed_this_tick, equity)
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
                        "markets": [snap.slug],
                        "quotes_placed": quotes_placed,
                        "cancels": cancels,
                        "fill_count": len(fill_snapshots),
                        "fills": fill_snapshots[-50:],
                        "positions": position_snapshots[-1] if position_snapshots else {},
                        "starting_cash": cash,
                        "ending_cash": port.cash,
                        "maker": port.to_dict({snap.slug: snap.mid}),
                        "trading": last_trading,
                        "account_risk": last_risk,
                        "account_risk_cap": format_risk_pct(config.paper.risk.max_account_risk_pct),
                        "market_risk_cap": format_score(config.paper.risk.max_market_risk_score),
                        "market_risk": last_scores,
                        "daily_limits": last_daily.as_dict() if last_daily else {},
                        "daily_loss_limit_hit": bool(last_daily and last_daily.loss_halted),
                        "strategy": favorites.STRATEGY if fav_mode else "two_sided_maker",
                    },
                )
            except Exception:
                logger.log("demo_state_error", error="failed to persist live demo state", live=False, demo=True)

            if sleep and tick < n_ticks - 1:
                time.sleep(config.paper.poll_interval_seconds)
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
    state = {
        "source": client.source_name,
        "venue": "kalshi",
        "demo": True,
        "live": False,
        "dry_run": False,
        "host": client.demo_base_url,
        "ticks": n_ticks,
        "markets": [snap.slug],
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
        "resting_alert": leftover_alert,
        "maker": port.to_dict({snap.slug: snap.mid}),
        "trading": last_trading,
        "account_risk": last_risk,
        "account_risk_cap": format_risk_pct(config.paper.risk.max_account_risk_pct),
        "market_risk_cap": format_score(config.paper.risk.max_market_risk_score),
        "market_risk": last_scores,
        "cancels_scoped": True,
        "daily_limits": last_daily.as_dict() if last_daily else {},
        "daily_loss_limit_hit": bool(last_daily and last_daily.loss_halted),
        "strategy": favorites.STRATEGY if favorites.is_favorites_mode(config) else "two_sided_maker",
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
