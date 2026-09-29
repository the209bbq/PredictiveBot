"""CLI: scan, paper, compare, Kalshi demo-order (opt-in), live stub."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from polymarket_bot.account_risk import AccountRiskError, format_risk_pct
from polymarket_bot.compare import collect_snapshots, compare_snapshots, format_compare_report
from polymarket_bot.config import load_config
from polymarket_bot.dashboard import serve_dashboard
from polymarket_bot.demo.session import assert_demo_order_within_risk, format_demo_report, run_demo_session
from polymarket_bot.market_risk import MarketRiskError, format_score
from polymarket_bot.exchanges.factory import COMPARE_DISABLED, build_client, compare_is_enabled
from polymarket_bot.exchanges.kalshi import KalshiClient
from polymarket_bot.guard import DemoOrderError, LiveTradingDisabled
from polymarket_bot.live import start_live_trading
from polymarket_bot.logging_utils import DecisionLogger, json_default
from polymarket_bot.paper.engine import run_paper
from polymarket_bot.paper.report import format_report
from polymarket_bot.recorder import run_record
from polymarket_bot import favorites
from polymarket_bot.scanner import format_scan_table, scan_markets
from polymarket_bot.trading import (
    TradingLock,
    TradingLockHeld,
    TradingPaused,
    set_trading_enabled,
    trading_is_on,
    trading_status,
)


def cmd_scan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    client = build_client(config, source=args.source, exchange=args.exchange, fixture=args.fixture)
    try:
        rows = scan_markets(client, config)
        hours = client.trading_hours() if hasattr(client, "trading_hours") else None
    finally:
        client.close()
    print(f"Exchange: {getattr(client, 'venue', args.exchange or config.exchange)}")
    print(f"Data source: {client.source_name}")
    print("Dry-run / read-only.\n")
    if hours:
        print("Trading hours (GET /exchange/schedule):")
        print(json.dumps(hours, default=str)[:800], "\n")
    print(format_scan_table(rows))
    if args.json:
        payload = [
            {
                "venue": s.venue,
                "slug": s.slug,
                "question": s.question,
                "mid": s.mid,
                "spread": s.spread,
                "best_bid": s.best_bid,
                "best_ask": s.best_ask,
                "bid_depth": s.bid_depth_contracts,
                "ask_depth": s.ask_depth_contracts,
                "volume_shares": s.volume_shares,
                "hours_to_resolution": s.hours_to_resolution,
                "risk_score": s.risk_score,
                "risk_score_display": format_score(s.risk_score),
                "risk_components": dict(s.risk_components or {}),
            }
            for s in rows
        ]
        print("\n" + json.dumps(payload, default=json_default, indent=2))
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    client = build_client(config, source=args.source, exchange=args.exchange, fixture=args.fixture)
    logger = DecisionLogger(config.logging.jsonl_path, config.logging.level)
    try:
        state = run_paper(client, config, logger, ticks=args.ticks, sleep=not args.no_sleep)
    finally:
        logger.close()
        client.close()
    report = format_report(state)
    config.logging.report_path.parent.mkdir(parents=True, exist_ok=True)
    config.logging.state_path.parent.mkdir(parents=True, exist_ok=True)
    config.logging.report_path.write_text(report)
    config.logging.state_path.write_text(json.dumps(state, default=json_default, indent=2))
    print(report)
    print(f"Wrote {config.logging.report_path} and {config.logging.state_path}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    path = Path(args.state or config.logging.state_path)
    print(format_report(json.loads(path.read_text())))
    return 0


def cmd_favorites_report(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    path = Path(args.fills) if args.fills else favorites.favorites_fills_path(config)
    rows = favorites.load_fill_rows(path)
    if args.reconcile:
        client = KalshiClient(config, public_only=True) if args.live_reconcile else None
        try:
            rows = favorites.reconcile_fills(rows, client=client)
            favorites.rewrite_fills(path, rows)
        finally:
            if client is not None:
                client.close()
    summary = favorites.summarize_fills(rows)
    report = favorites.format_favorites_report(summary)
    print(report)
    if args.csv:
        csv_path = Path(args.csv)
        favorites.write_csv(rows, csv_path)
        print(f"Wrote {csv_path}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if not compare_is_enabled(config):
        print(COMPARE_DISABLED, file=sys.stderr)
        return 2
    kalshi = build_client(config, source=args.kalshi_source, exchange="kalshi", fixture=args.kalshi_fixture)
    pm = build_client(
        config,
        source=args.pm_source,
        exchange="polymarket_us",
        fixture=args.pm_fixture,
    )
    try:
        k_rows = collect_snapshots(kalshi, config)
        p_rows = collect_snapshots(pm, config)
    finally:
        kalshi.close()
        pm.close()
    gaps = compare_snapshots(k_rows, p_rows, config)
    report = format_compare_report(gaps, config)
    out = Path("logs/compare_report.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report)
    print(report)
    print(f"Wrote {out}")
    return 0


def cmd_kalshi_demo(args: argparse.Namespace) -> int:
    config = load_config(args.config, environment="demo")
    on, reason = trading_is_on(config)
    if not on:
        print(f"Trading is off ({reason}). Run: pmbot trading on", file=sys.stderr)
        return 2
    client = KalshiClient(config)
    lock = TradingLock(config.trading.lock_path)
    try:
        lock.acquire()
        frac = assert_demo_order_within_risk(
            client,
            config,
            ticker=args.ticker,
            side=args.side,
            price=args.price,
            count=args.count,
            confirm_demo=args.confirm_demo,
        )
        result = client.place_demo_order(
            ticker=args.ticker,
            side=args.side,
            price=args.price,
            count=args.count,
            confirm_demo=args.confirm_demo,
        )
    finally:
        lock.release()
        client.close()
    print(json.dumps(result, indent=2, default=str))
    print(f"Account risk after this order (if filled): {format_risk_pct(frac)}")
    print("Posted to Kalshi DEMO only. Production trading remains disabled.")
    return 0


def cmd_kalshi_demo_session(args: argparse.Namespace) -> int:
    config = load_config(args.config, environment="demo")
    # Books and orders both hit the demo host so tickers exist there.
    client = KalshiClient(config, data_base_url=config.kalshi.demo_base_url)
    if getattr(args, "emergency_cancel_all", False):
        lock = TradingLock(config.trading.lock_path)
        try:
            lock.acquire()
            remaining = client.shutdown_demo_orders(
                confirm_demo=args.confirm_demo,
                emergency_all=True,
            )
        finally:
            lock.release()
            client.close()
        print("Emergency account-wide cancel completed (every resting DEMO order).")
        print(f"Resting leftover: {remaining or 'none'}")
        return 0
    logger = DecisionLogger(Path("logs/demo_decisions.jsonl"), config.logging.level)
    try:
        state = run_demo_session(
            client,
            config,
            logger,
            confirm_demo=args.confirm_demo,
            ticks=args.ticks,
            ticker=args.ticker,
            sleep=not args.no_sleep,
        )
    finally:
        logger.close()
        client.close()
    report = format_demo_report(state)
    report_path = Path("logs/demo_report.txt")
    state_path = config.logging.demo_state_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report)
    state_path.write_text(json.dumps(state, default=json_default, indent=2))
    print(report)
    print(f"Wrote {report_path} and {state_path}")
    if state.get("resting_alert") or state.get("resting_leftover"):
        print(state.get("resting_alert") or "ALERT: resting Kalshi DEMO orders remain.", file=sys.stderr)
        return 2
    return 0


def cmd_trading(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    path = config.trading.toggle_path
    if args.action == "on":
        set_trading_enabled(path, True)
        print(f"Trading ON (wrote {path})")
        print("A running loop will resume quoting on its next iteration.")
        return 0
    if args.action == "off":
        set_trading_enabled(path, False)
        print(f"Trading OFF (wrote {path})")
        print("A running loop will cancel resting orders and stay read-only.")
        return 0
    status = trading_status(config)
    print(f"Trading: {status['effective']} ({status['reason']})")
    print(f"  config.trading.enabled: {status['config_enabled']}")
    print(f"  toggle file: {status['toggle_file'] or 'absent (treated as on)'}  ({status['toggle_path']})")
    print(f"  lock file: {status['lock_path']}")
    print(
        f"  Daily capital in use: ${status['daily_capital_in_use_usd']} / "
        f"${status['max_daily_capital_in_use_usd']}"
    )
    hit = "daily loss limit hit" if status["daily_loss_limit_hit"] else "ok"
    print(
        f"  Daily P&L (PT {status['pt_date']}): ${status['daily_pnl_usd']} / "
        f"limit -${status['max_daily_loss_usd']} ({hit})"
    )
    return 0 if status["effective"] == "on" else 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    host = args.host or config.dashboard.host
    port = args.port if args.port is not None else config.dashboard.port
    object.__setattr__(config.dashboard, "host", host)
    object.__setattr__(config.dashboard, "port", port)
    serve_dashboard(config)
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    source = args.source
    if source == "live" and config.record.data_host == "demo":
        source = "kalshi-demo-data"
    client = build_client(
        config,
        source=source,
        exchange="kalshi",
        fixture=args.fixture,
        public_only=True,
    )
    try:
        path = run_record(
            config,
            client,
            ticks=args.ticks,
            sleep=not args.no_sleep,
        )
    finally:
        client.close()
    print(f"Wrote recording {path}")
    return 0


def cmd_live(_args: argparse.Namespace) -> int:
    start_live_trading()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pmbot",
        description="Kalshi paper bot. Production trading disabled.",
    )
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    scan = sub.add_parser("scan", help="List liquid markets (public data)")
    scan.add_argument("--exchange", default=None, help="kalshi (default). polymarket_us is disabled.")
    scan.add_argument("--source", default="live", choices=["live", "fixture", "replay", "kalshi-demo-data"])
    scan.add_argument("--fixture", default=None)
    scan.add_argument("--json", action="store_true")
    scan.set_defaults(func=cmd_scan)

    paper = sub.add_parser("paper", help="Simulated paper session")
    paper.add_argument("--exchange", default=None, help="kalshi (default). polymarket_us is disabled.")
    paper.add_argument("--source", default="replay", choices=["live", "fixture", "replay", "kalshi-demo-data"])
    paper.add_argument("--fixture", default=None)
    paper.add_argument("--ticks", type=int, default=None)
    paper.add_argument("--no-sleep", action="store_true")
    paper.set_defaults(func=cmd_paper)

    report = sub.add_parser("report", help="Print last paper report")
    report.add_argument("--state", default=None)
    report.set_defaults(func=cmd_report)

    fav = sub.add_parser("favorites-report", help="Favorites strategy fill log, Wilson CI, optional CSV")
    fav.add_argument("--fills", default=None, help="JSONL path (default data/favorites_fills.jsonl)")
    fav.add_argument("--csv", default=None, help="Write a CSV export")
    fav.add_argument("--reconcile", action="store_true", help="Rewrite settlement / post-fill mids on the log")
    fav.add_argument(
        "--live-reconcile",
        action="store_true",
        help="Poll public Kalshi markets for settlement (no keys, no orders)",
    )
    fav.set_defaults(func=cmd_favorites_report)

    compare = sub.add_parser(
        "compare",
        help="Disabled. Kalshi vs Polymarket US gaps (requires compare.enabled)",
    )
    compare.add_argument("--kalshi-source", default="live")
    compare.add_argument("--pm-source", default="live")
    compare.add_argument("--kalshi-fixture", default=None)
    compare.add_argument("--pm-fixture", default=None)
    compare.set_defaults(func=cmd_compare)

    demo = sub.add_parser("kalshi-demo-order", help="Opt-in single order on Kalshi DEMO only")
    demo.add_argument("--ticker", required=True)
    demo.add_argument("--side", choices=["bid", "ask"], default="bid")
    demo.add_argument("--price", default="0.0100")
    demo.add_argument("--count", default="1")
    demo.add_argument("--confirm-demo", action="store_true")
    demo.set_defaults(func=cmd_kalshi_demo)

    demo_session = sub.add_parser(
        "kalshi-demo",
        help="Opt-in Kalshi DEMO session (quote / re-quote / cancel + report)",
    )
    demo_session.add_argument("--ticker", default=None, help="Demo ticker; otherwise scan demo books")
    demo_session.add_argument("--ticks", type=int, default=None)
    demo_session.add_argument("--no-sleep", action="store_true")
    demo_session.add_argument("--confirm-demo", action="store_true")
    demo_session.add_argument(
        "--emergency-cancel-all",
        action="store_true",
        help="Cancel EVERY resting order on the demo account (not just this bot's). Emergency only.",
    )
    demo_session.set_defaults(func=cmd_kalshi_demo_session)

    trading = sub.add_parser("trading", help="Turn order placement on or off without a code change")
    trading.add_argument("action", choices=["on", "off", "status"])
    trading.set_defaults(func=cmd_trading)

    dash = sub.add_parser("dashboard", help="Local web page for status, risk, P&L, and the trading toggle")
    dash.add_argument("--host", default=None, help="Bind address (default 127.0.0.1)")
    dash.add_argument("--port", type=int, default=None, help="Port (default 8787)")
    dash.set_defaults(func=cmd_dashboard)

    record = sub.add_parser("record", help="Read-only Kalshi book + tape recorder")
    record.add_argument("--source", default="live", choices=["live", "fixture", "replay", "kalshi-demo-data"])
    record.add_argument("--fixture", default=None)
    record.add_argument("--ticks", type=int, default=1)
    record.add_argument("--no-sleep", action="store_true")
    record.set_defaults(func=cmd_record)

    live = sub.add_parser("live", help="Disabled. Always raises.")
    live.set_defaults(func=cmd_live)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (
        LiveTradingDisabled,
        DemoOrderError,
        TradingLockHeld,
        TradingPaused,
        AccountRiskError,
        MarketRiskError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 2
