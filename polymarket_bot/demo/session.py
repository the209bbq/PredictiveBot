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
from polymarket_bot.paper import maker
from polymarket_bot.paper.portfolio import Portfolio
from polymarket_bot.scanner import scan_markets


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


def _sync_portfolio(port: Portfolio, positions: dict[str, Any] | list) -> None:
    for row in _position_rows(positions):
        ticker = str(row.get("ticker") or row.get("market_ticker") or "")
        if not ticker:
            continue
        qty = _as_decimal(row.get("position") or row.get("quantity") or row.get("qty"))
        if qty is None:
            continue
        port.position(ticker).qty = qty


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

    logger.log(
        "demo_start",
        host=client.demo_base_url,
        market=snap.slug,
        ticks=n_ticks,
        live=False,
        demo=True,
    )

    try:
        for tick in range(n_ticks):
            snap = _refresh_snapshot(client, snap, logger)
            try:
                client.cancel_all_demo_orders(confirm_demo=True)
                cancels += 1
                logger.log("cancel", reason="requote", market=snap.slug, live=False, demo=True)
            except DemoOrderError as exc:
                logger.log("cancel_error", error=str(exc), market=snap.slug, live=False)

            if snap.stale or not snap.book_fetched:
                logger.log("skip_quote", reason="stale_book", market=snap.slug, live=False)
            else:
                try:
                    pos_payload = client.demo_positions(confirm_demo=True)
                    _sync_portfolio(port, pos_payload)
                    position_snapshots.append(pos_payload)
                except DemoOrderError as exc:
                    logger.log("position_error", error=str(exc), live=False)

                desired = maker.desired_quotes(snap, port, config, f"d{tick}")
                for order in desired:
                    side = "bid" if order.side == "buy" else "ask"
                    try:
                        result = client.place_demo_order(
                            ticker=snap.slug,
                            side=side,
                            price=str(order.price),
                            count=str(int(order.qty)),
                            confirm_demo=True,
                        )
                        quotes_placed += 1
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
                port.record_equity({snap.slug: snap.mid})
            except DemoOrderError as exc:
                logger.log("balance_error", error=str(exc), live=False)

            if sleep and tick < n_ticks - 1:
                time.sleep(config.paper.poll_interval_seconds)
    finally:
        try:
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
    }
    logger.log(
        "demo_end",
        quotes_placed=quotes_placed,
        fills=len(fill_snapshots),
        leftover=bool(leftover_alert or remaining),
        live=False,
        demo=True,
    )
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
        f"Cancel-all rounds: {state.get('cancels', 0)}",
        f"Fills reported by demo API: {state.get('fill_count', 0)}",
        f"Starting balance/cash: {_money(state.get('starting_cash'))}",
        f"Ending balance/cash: {_money(state.get('ending_cash'))}",
        "",
        "Positions",
    ]
    if pos_rows:
        for row in pos_rows:
            ticker = row.get("ticker") or row.get("market_ticker") or "?"
            qty = row.get("position") or row.get("quantity") or row.get("qty")
            lines.append(f"  {ticker}: qty={qty}")
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
