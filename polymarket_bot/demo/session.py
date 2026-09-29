"""Opt-in Kalshi DEMO session: quote, re-quote, cancel, then verify flat.

Talks only to a Kalshi demo host. Production trading stays disabled.
Secrets come from environment variables. No transfer endpoints are called.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from polymarket_bot.config import AppConfig
from polymarket_bot.exchanges.kalshi import KalshiClient
from polymarket_bot.guard import DemoOrderError
from polymarket_bot.logging_utils import DecisionLogger
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
from polymarket_bot.paper.risk import (
    past_resolution_cutoff,
    snapshot_account_risk,
    would_breach_account_risk,
)
from polymarket_bot.pnl import append_pnl
from polymarket_bot.scanner import scan_markets
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
    for key in (
        "balance",
        "available_balance",
        "available",
        "portfolio_value",
        "cash",
    ):
        parsed = _as_decimal(payload.get(key))
        if parsed is not None:
            return parsed
    return None


def _position_rows(payload: dict[str, Any] | list) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("market_positions", "positions", "event_positions"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _account_value_from_exchange(
    balance: dict[str, Any],
    positions: dict[str, Any] | list,
    mids: dict[str, Decimal | None],
) -> Decimal:
    for key in ("portfolio_value", "equity", "account_value"):
        parsed = _as_decimal(balance.get(key))
        if parsed is not None and parsed > 0:
            return parsed
    cash = _balance_available(balance) or Decimal("0")
    marked = Decimal("0")
    for row in _position_rows(positions):
        ticker = str(row.get("ticker") or row.get("market_ticker") or "")
        qty = _as_decimal(row.get("position") or row.get("quantity") or row.get("qty"))
        if not ticker or qty is None:
            continue
        mid = mids.get(ticker)
        if mid is None:
            mid = _as_decimal(row.get("market_exposure") or row.get("average_price"))
        if mid is None:
            continue
        marked += qty * mid
    return cash + marked


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
    avg = _as_decimal(row.get("average_price") or row.get("avg_price") or row.get("avg_px"))
    if avg is not None:
        return abs(avg)
    exposure = _as_decimal(row.get("market_exposure") or row.get("exposure"))
    if exposure is not None and qty != 0:
        return abs(exposure / qty)
    return None


def _sync_portfolio(port: Portfolio, positions: dict[str, Any] | list) -> None:
    """Mirror every exchange position (including leftovers from earlier sessions)."""
    seen: set[str] = set()
    for row in _position_rows(positions):
        ticker = str(row.get("ticker") or row.get("market_ticker") or "")
        if not ticker:
            continue
        qty = _as_decimal(row.get("position") or row.get("quantity") or row.get("qty"))
        if qty is None:
            continue
        pos = port.position(ticker)
        pos.qty = qty
        avg = _position_avg_price(row, qty)
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
    picks = scan_markets(client, config, now=now)
    if not picks:
        raise DemoOrderError("No liquid Kalshi DEMO market matched the scanner filters.")
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
        q = _as_decimal(row.get("position") or row.get("quantity") or row.get("qty"))
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

            skip_reason = None
            if not trading_on:
                skip_reason = "trading_off"
            elif snap.stale or not snap.book_fetched:
                skip_reason = "stale_book"
            elif is_live_in_game(snap):
                skip_reason = "live_in_game"
            elif market_over_risk_threshold(snap, config.paper.risk.max_market_risk_score):
                skip_reason = "market_risk_score"
            elif past_resolution_cutoff(snap, config.paper.risk.maker_min_hours_to_resolution):
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
            else:
                desired = maker.desired_quotes(snap, port, config, f"d{tick}")
                for order in desired:
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
                    if would_breach_account_risk(
                        port,
                        placed_this_tick,
                        order,
                        equity,
                        config.paper.risk.max_account_risk_pct,
                    ):
                        logger.log(
                            "skip_quote",
                            reason="account_risk_cap",
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
                        )
                        quotes_placed += 1
                        placed_this_tick.append(order)
                        logger.log(
                            "quote",
                            strategy="maker",
                            market=order.market,
                            side=side,
                            price=order.price,
                            qty=order.qty,
                            order_id=_order_id(result),
                            live=False,
                            demo=True,
                        )
                    except DemoOrderError as exc:
                        logger.log("quote_error", error=str(exc), market=snap.slug, side=side, live=False)

            try:
                fills = client.demo_fills(confirm_demo=True)
                fill_snapshots = fills
                logger.log("fills_snapshot", count=len(fills), live=False, demo=True)
            except DemoOrderError as exc:
                logger.log("fills_error", error=str(exc), live=False)

            try:
                bal = client.demo_balance(confirm_demo=True)
                avail = _balance_available(bal)
                if avail is not None:
                    port.cash = avail
                mids = {snap.slug: snap.mid}
                for slug, pos in port.positions.items():
                    if slug not in mids or mids[slug] is None:
                        mids[slug] = pos.avg_price or None
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
            except DemoOrderError as exc:
                logger.log("balance_error", error=str(exc), live=False)

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
            qty = row.get("position") or row.get("quantity") or row.get("qty")
            avg = row.get("average_price") or row.get("avg_price") or row.get("avg_px")
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
                f"{fill.get('side') or ''} {fill.get('count') or fill.get('quantity') or ''} "
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
