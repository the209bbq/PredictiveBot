"""CLI: scan, paper, compare, Kalshi demo-order (opt-in), live stub."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from polymarket_bot.compare import collect_snapshots, compare_snapshots, format_compare_report
from polymarket_bot.config import load_config
from polymarket_bot.demo.session import format_demo_report, run_demo_session
from polymarket_bot.exchanges.factory import build_client
from polymarket_bot.exchanges.kalshi import KalshiClient
from polymarket_bot.guard import DemoOrderError, LiveTradingDisabled
from polymarket_bot.live import start_live_trading
from polymarket_bot.logging_utils import DecisionLogger, json_default
from polymarket_bot.paper.engine import run_paper
from polymarket_bot.paper.report import format_report
from polymarket_bot.scanner import format_scan_table, scan_markets


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


def cmd_compare(args: argparse.Namespace) -> int:
    config = load_config(args.config)
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
    config = load_config(args.config)
    client = KalshiClient(config)
    try:
        result = client.place_demo_order(
            ticker=args.ticker,
            side=args.side,
            price=args.price,
            count=args.count,
            confirm_demo=args.confirm_demo,
        )
    finally:
        client.close()
    print(json.dumps(result, indent=2, default=str))
    print("Posted to Kalshi DEMO only. Production trading remains disabled.")
    return 0


def cmd_kalshi_demo_session(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    # Books and orders both hit the demo host so tickers exist there.
    client = KalshiClient(config, data_base_url=config.kalshi.demo_base_url)
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
    state_path = Path("logs/demo_state.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report)
    state_path.write_text(json.dumps(state, default=json_default, indent=2))
    print(report)
    print(f"Wrote {report_path} and {state_path}")
    if state.get("resting_alert") or state.get("resting_leftover"):
        print(state.get("resting_alert") or "ALERT: resting Kalshi DEMO orders remain.", file=sys.stderr)
        return 2
    return 0


def cmd_live(_args: argparse.Namespace) -> int:
    start_live_trading()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pmbot",
        description="Kalshi-primary paper bot with Polymarket US as a second venue. Production trading disabled.",
    )
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    scan = sub.add_parser("scan", help="List liquid markets (public data)")
    scan.add_argument("--exchange", default=None, help="kalshi (default) or polymarket_us")
    scan.add_argument("--source", default="live", choices=["live", "fixture", "replay", "kalshi-demo-data"])
    scan.add_argument("--fixture", default=None)
    scan.add_argument("--json", action="store_true")
    scan.set_defaults(func=cmd_scan)

    paper = sub.add_parser("paper", help="Simulated paper session")
    paper.add_argument("--exchange", default=None, help="kalshi (default) or polymarket_us")
    paper.add_argument("--source", default="replay", choices=["live", "fixture", "replay", "kalshi-demo-data"])
    paper.add_argument("--fixture", default=None)
    paper.add_argument("--ticks", type=int, default=None)
    paper.add_argument("--no-sleep", action="store_true")
    paper.set_defaults(func=cmd_paper)

    report = sub.add_parser("report", help="Print last paper report")
    report.add_argument("--state", default=None)
    report.set_defaults(func=cmd_report)

    compare = sub.add_parser("compare", help="Read-only Kalshi vs Polymarket US fee-adjusted gaps")
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
    demo_session.set_defaults(func=cmd_kalshi_demo_session)

    live = sub.add_parser("live", help="Disabled. Always raises.")
    live.set_defaults(func=cmd_live)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (LiveTradingDisabled, DemoOrderError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
