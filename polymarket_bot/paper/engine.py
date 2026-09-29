"""Paper-trading loop. Simulated quotes only; no live orders."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from polymarket_bot.config import AppConfig
from polymarket_bot.logging_utils import DecisionLogger
from polymarket_bot.market_data import MarketDataClient, MarketSnapshot
from polymarket_bot.market_data.errors import BookFetchError
from polymarket_bot.market_data.replay_client import ReplayClient
from polymarket_bot.paper import maker, near_resolution
from polymarket_bot.paper.fills import fill_qty, synthesize_tape
from polymarket_bot.paper.portfolio import PaperOrder, Portfolio
from polymarket_bot.market_risk import (
    PriceHistory,
    attach_market_risk,
    format_score,
    market_over_risk_threshold,
    score_payload,
)
from polymarket_bot.paper.risk import (
    account_value_from_portfolio,
    check_daily_loss,
    format_risk_pct,
    past_resolution_cutoff,
    price_jumped,
    snapshot_account_risk,
)
from polymarket_bot.scanner import scan_markets, scan_near_resolution
from polymarket_bot.series_filter import maker_min_hours, maker_universe_ok
from polymarket_bot.pnl import append_pnl
from polymarket_bot.trading import TradingLock, trading_is_on


def _mids(snaps: dict[str, MarketSnapshot]) -> dict[str, Decimal | None]:
    return {slug: snap.mid for slug, snap in snaps.items()}


def _cancel_all(orders: list[PaperOrder], logger: DecisionLogger, reason: str, strategy: str) -> list[PaperOrder]:
    for order in orders:
        logger.log("cancel", strategy=strategy, market=order.market, side=order.side, reason=reason, live=False)
    return []


def _mark_stale(snap: MarketSnapshot) -> MarketSnapshot:
    snap.stale = True
    snap.book_fetched = False
    return snap


def _unix(ts: datetime | None) -> int | None:
    if ts is None:
        return None
    return int(ts.timestamp())


def _attach_tape(
    client: MarketDataClient,
    snap: MarketSnapshot,
    logger: DecisionLogger,
    since: datetime | None,
) -> MarketSnapshot:
    getter = getattr(client, "trades", None)
    if callable(getter):
        try:
            snap.tape = getter(snap.slug, min_ts=_unix(since) if since else None)
            if snap.tape:
                snap.last_trade = snap.tape[-1].price
            return snap
        except (BookFetchError, Exception, TypeError) as exc:
            logger.log("tape_error", market=snap.slug, error=str(exc), live=False)
    last_fn = getattr(client, "last_trade", None)
    if callable(last_fn):
        try:
            last = last_fn(snap.slug)
            if last is not None:
                snap.last_trade = last
        except (BookFetchError, Exception) as exc:
            logger.log("last_trade_error", market=snap.slug, error=str(exc), live=False)
    return snap


def _refresh_live(
    client: MarketDataClient,
    snap: MarketSnapshot,
    logger: DecisionLogger,
    now: datetime,
    since: datetime | None = None,
) -> MarketSnapshot:
    """Fetch a fresh book and the trade tape since the last poll."""
    try:
        book = client.book(snap.slug)
    except (BookFetchError, Exception) as exc:
        logger.log("book_error", market=snap.slug, error=str(exc), stale=True, live=False)
        return _mark_stale(snap)

    market = snap.raw.get("market") or {
        "slug": snap.slug,
        "ticker": snap.slug,
        "question": snap.question,
    }
    refreshed = client.snapshot(market, book, now=now)
    refreshed = _attach_tape(client, refreshed, logger, since)
    refreshed.stale = False
    refreshed.book_fetched = True
    return refreshed


def _choose_markets(
    client: MarketDataClient,
    config: AppConfig,
    now: datetime,
    markets: list[MarketSnapshot] | None,
) -> list[MarketSnapshot]:
    if markets is not None:
        return list(markets)
    maker_picks = [
        s for s in scan_markets(client, config, now=now) if maker_universe_ok(s, config, now=now)
    ][: config.paper.max_markets]
    near_picks = scan_near_resolution(client, config, now=now)
    seen: set[str] = set()
    chosen: list[MarketSnapshot] = []
    for snap in maker_picks + near_picks:
        if snap.slug in seen:
            continue
        seen.add(snap.slug)
        chosen.append(snap)
    return chosen


def run_paper(
    client: MarketDataClient,
    config: AppConfig,
    logger: DecisionLogger,
    *,
    ticks: int | None = None,
    sleep: bool = True,
    now: datetime | None = None,
    markets: list[MarketSnapshot] | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    n_ticks = ticks if ticks is not None else config.paper.ticks
    maker_port = Portfolio("maker", config.paper.starting_cash, config.paper.starting_cash)
    near_port = Portfolio("near_resolution", config.paper.starting_cash, config.paper.starting_cash)

    chosen = _choose_markets(client, config, now, markets)
    lock = TradingLock(config.trading.lock_path)
    lock.acquire()
    try:
        return _run_paper_locked(
            client,
            config,
            logger,
            n_ticks=n_ticks,
            sleep=sleep,
            now=now,
            chosen=chosen,
            maker_port=maker_port,
            near_port=near_port,
        )
    finally:
        lock.release()


def _run_paper_locked(
    client: MarketDataClient,
    config: AppConfig,
    logger: DecisionLogger,
    *,
    n_ticks: int,
    sleep: bool,
    now: datetime,
    chosen: list[MarketSnapshot],
    maker_port: Portfolio,
    near_port: Portfolio,
) -> dict[str, Any]:
    maker_book: list[PaperOrder] = []
    near_book: list[PaperOrder] = []
    prev: dict[str, MarketSnapshot] = {}
    last_risk: dict[str, dict[str, Any]] = {}
    last_scores: dict[str, dict[str, Any]] = {}
    last_quote_at: dict[str, datetime] = {}
    last_tape_at: dict[str, datetime] = {}
    history = PriceHistory()
    logger.log(
        "paper_start",
        source=client.source_name,
        live=False,
        dry_run=True,
        markets=[m.slug for m in chosen],
        ticks=n_ticks,
    )

    is_replay = isinstance(client, ReplayClient)

    for tick in range(n_ticks):
        snaps: dict[str, MarketSnapshot] = {}
        universe: list[MarketSnapshot]
        if is_replay:
            listed = client.list_markets(limit=config.scanner.max_markets_to_list, active=True, closed=False)
            universe = []
            for market in listed:
                slug = market.get("slug") or market.get("ticker")
                snap = client.snapshot(market, client.book(slug), now=now)
                snap.stale = False
                snap.book_fetched = True
                universe.append(snap)
        else:
            universe = [
                _refresh_live(
                    client,
                    snap,
                    logger,
                    datetime.now(timezone.utc),
                    since=last_tape_at.get(snap.slug),
                )
                for snap in chosen
            ]
            chosen = universe

        for snap in universe:
            if not snap.tape:
                snap.tape = synthesize_tape(snap, prev.get(snap.slug))
            if snap.tape and snap.tape[-1].ts:
                last_tape_at[snap.slug] = snap.tape[-1].ts
            else:
                last_tape_at[snap.slug] = datetime.now(timezone.utc)
            attach_market_risk(snap, history)
            snaps[snap.slug] = snap
            last_scores[snap.slug] = score_payload(snap)
            logger.log(
                "market_risk",
                market=snap.slug,
                score=format_score(snap.risk_score),
                live_game=bool((snap.risk_components or {}).get("live_game")),
                live=False,
            )

        mids = _mids(snaps)
        for port in (maker_port, near_port):
            port.record_equity(mids)
            reason = check_daily_loss(port, mids, config.paper.risk)
            if reason and not port.killed:
                port.killed = True
                port.kill_reason = reason
                logger.log("kill_switch", strategy=port.name, reason=reason, live=False)

        if maker_port.killed:
            maker_book = _cancel_all(maker_book, logger, maker_port.kill_reason or "killed", "maker")
        if near_port.killed:
            near_book = _cancel_all(near_book, logger, near_port.kill_reason or "killed", "near_resolution")

        def process_book(book: list[PaperOrder], port: Portfolio) -> list[PaperOrder]:
            remaining: list[PaperOrder] = []
            if port.killed:
                return remaining
            for order in book:
                snap = snaps.get(order.market)
                if snap is None:
                    remaining.append(order)
                    continue
                if snap.stale or not snap.book_fetched:
                    logger.log(
                        "cancel",
                        strategy=order.strategy,
                        market=order.market,
                        reason="stale_book",
                        live=False,
                    )
                    continue
                if market_over_risk_threshold(snap, config.paper.risk.max_market_risk_score):
                    logger.log(
                        "cancel",
                        strategy=order.strategy,
                        market=order.market,
                        reason="market_risk_score",
                        score=format_score(snap.risk_score),
                        live=False,
                    )
                    continue
                if price_jumped(prev.get(order.market), snap, config.paper.risk.cancel_on_price_jump):
                    logger.log(
                        "cancel",
                        strategy=order.strategy,
                        market=order.market,
                        reason="price_jump",
                        live=False,
                    )
                    continue
                min_hours = (
                    maker_min_hours(snap, config)
                    if order.strategy == "maker"
                    else config.paper.near_resolution.min_hours_to_resolution
                )
                if past_resolution_cutoff(snap, min_hours):
                    logger.log(
                        "cancel",
                        strategy=order.strategy,
                        market=order.market,
                        reason="resolution_cutoff",
                        live=False,
                    )
                    continue
                qty, reason = fill_qty(
                    order,
                    snap,
                    prev.get(order.market),
                    strict=config.paper.fills.require_strict_trade_through,
                )
                if reason and qty > 0:
                    fill = port.apply_fill(order, qty, reason)
                    logger.log(
                        "fill",
                        strategy=order.strategy,
                        market=order.market,
                        side=order.side,
                        price=order.price,
                        qty=qty,
                        rebate=fill.rebate,
                        reason=reason,
                        live=False,
                    )
                    leftover = order.qty - qty
                    if leftover > 0:
                        remaining.append(
                            PaperOrder(
                                order_id=order.order_id,
                                market=order.market,
                                side=order.side,
                                price=order.price,
                                qty=leftover,
                                strategy=order.strategy,
                                venue=order.venue,
                                fee_type=order.fee_type,
                                fee_multiplier=order.fee_multiplier,
                            )
                        )
                    continue
                remaining.append(order)
            return remaining

        trading_on, trading_reason = trading_is_on(config)
        if not trading_on:
            maker_book = _cancel_all(maker_book, logger, "trading_off", "maker")
            near_book = _cancel_all(near_book, logger, "trading_off", "near_resolution")
            logger.log("trading_paused", reason=trading_reason, live=False)
        else:
            maker_book = process_book(maker_book, maker_port)
            near_book = process_book(near_book, near_port)

            now_tick = datetime.now(timezone.utc)
            interval = config.paper.maker.requote_interval_seconds
            touch_ticks = config.paper.maker.requote_on_touch_ticks
            prefix = f"t{tick}"

            def _replace(book: list[PaperOrder], port: Portfolio, strategy: str) -> list[PaperOrder]:
                if port.killed:
                    return []
                kept: list[PaperOrder] = []
                quoted: set[str] = set()
                for snap in universe:
                    tick_sz = snap.tick_size or config.paper.tick_size_fallback
                    if not maker.should_requote(
                        prev.get(snap.slug),
                        snap,
                        last_quote_at.get(f"{strategy}:{snap.slug}"),
                        now_tick,
                        interval,
                        tick_sz,
                        touch_ticks=touch_ticks,
                    ):
                        kept.extend([o for o in book if o.market == snap.slug])
                        continue
                    for order in book:
                        if order.market == snap.slug:
                            logger.log(
                                "cancel",
                                strategy=strategy,
                                market=order.market,
                                side=order.side,
                                reason="requote",
                                live=False,
                            )
                    if strategy == "maker":
                        quotes = maker.desired_quotes(snap, port, config, prefix, resting=kept)
                    else:
                        quotes = near_resolution.desired_quotes(
                            snap, port, config, prefix, resting=kept
                        )
                    for order in quotes:
                        kept.append(order)
                        quoted.add(snap.slug)
                        logger.log(
                            "quote",
                            strategy=strategy,
                            market=order.market,
                            side=order.side,
                            price=order.price,
                            qty=order.qty,
                            live=False,
                        )
                    last_quote_at[f"{strategy}:{snap.slug}"] = now_tick
                return kept

            maker_book = _replace(maker_book, maker_port, "maker")
            near_book = _replace(near_book, near_port, "near_resolution")

        cap = config.paper.risk.max_account_risk_pct
        for name, port, book in (
            ("maker", maker_port, maker_book),
            ("near_resolution", near_port, near_book),
        ):
            equity = account_value_from_portfolio(port, mids)
            at_risk, frac = snapshot_account_risk(port, book, equity)
            last_risk[name] = {
                "at_risk": at_risk,
                "equity": equity,
                "pct": frac,
                "pct_display": format_risk_pct(frac),
                "cap": cap,
                "cap_display": format_risk_pct(cap),
            }
            logger.log(
                "account_risk",
                strategy=name,
                pct=format_risk_pct(frac),
                at_risk=at_risk,
                equity=equity,
                cap=format_risk_pct(cap),
                live=False,
            )
        try:
            append_pnl(
                config.dashboard.pnl_path,
                {
                    "live": False,
                    "demo": False,
                    "maker": maker_port.to_dict(mids),
                    "near_resolution": near_port.to_dict(mids),
                },
            )
        except Exception:
            logger.log("pnl_error", error="failed to persist pnl history", live=False)

        prev = snaps
        if is_replay:
            client.advance()
        elif sleep and tick < n_ticks - 1:
            time.sleep(config.paper.poll_interval_seconds)

    maker_book = _cancel_all(maker_book, logger, "session_end", "maker")
    near_book = _cancel_all(near_book, logger, "session_end", "near_resolution")
    mids = _mids(prev)
    maker_port.record_equity(mids)
    near_port.record_equity(mids)
    maker_state = maker_port.to_dict(mids)
    near_state = near_port.to_dict(mids)
    if last_risk.get("maker"):
        maker_state["account_risk"] = last_risk["maker"]
    if last_risk.get("near_resolution"):
        near_state["account_risk"] = last_risk["near_resolution"]
    trading_on, trading_reason = trading_is_on(config)
    state = {
        "source": client.source_name,
        "venue": getattr(client, "venue", None),
        "dry_run": True,
        "live": False,
        "ticks": n_ticks,
        "markets": [s.slug for s in chosen],
        "maker": maker_state,
        "near_resolution": near_state,
        "trading": "on" if trading_on else "off",
        "trading_reason": trading_reason,
        "account_risk_cap": format_risk_pct(config.paper.risk.max_account_risk_pct),
        "market_risk_cap": format_score(config.paper.risk.max_market_risk_score),
        "market_risk": last_scores,
    }
    logger.log("paper_end", live=False, maker_pnl=state["maker"]["net_pnl"], near_pnl=state["near_resolution"]["net_pnl"])
    try:
        append_pnl(config.dashboard.pnl_path, state)
    except Exception:
        logger.log("pnl_error", error="failed to persist pnl history", live=False)
    return state
