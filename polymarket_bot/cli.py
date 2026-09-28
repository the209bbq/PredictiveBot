"""CLI: scan, paper, report, and a live stub that always raises."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from polymarket_bot.config import load_config
from polymarket_bot.guard import LiveTradingDisabled
from polymarket_bot.live import start_live_trading
from polymarket_bot.logging_utils import DecisionLogger, json_default
from polymarket_bot.market_data.fixture_client import FixtureClient
from polymarket_bot.market_data.replay_client import ReplayClient
from polymarket_bot.market_data.sdk_client import SdkPublicClient
from polymarket_bot.paper.engine import run_paper
from polymarket_bot.paper.report import format_report
from polymarket_bot.scanner import format_scan_table, scan_markets


def _client(source: str, config, fixture: str | None):
    if source == "live":
        return SdkPublicClient(config)
    if source == "fixture":
        path = Path(fixture or "fixtures/public_markets.json")
        return FixtureClient(path)
    if source == "replay":
        path = Path(fixture or "fixtures/replay.json")
        return ReplayClient(path)
    raise SystemExit(f"unknown source {source}")


def cmd_scan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    client = _client(args.source, config, args.fixture)
    try:
        rows = scan_markets(client, config)
    finally:
        client.close()
    print(f"Data source: {client.source_name}")
    print(f"Dry-run / read-only. {len(rows)} liquid markets.\n")
    print(format_scan_table(rows))
    if args.json:
        payload = [
            {
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
    client = _client(args.source, config, args.fixture)
    logger = DecisionLogger(config.logging.jsonl_path, config.logging.level)
    try:
        state = run_paper(
            client,
            config,
            logger,
            ticks=args.ticks,
            sleep=not args.no_sleep,
        )
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
    print(f"Decision log: {config.logging.jsonl_path}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    path = Path(args.state or config.logging.state_path)
    state = json.loads(path.read_text())
    print(format_report(state))
    return 0


def cmd_live(_args: argparse.Namespace) -> int:
    start_live_trading()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pmbot",
        description="Dry-run Polymarket US scanner and paper bot. Never places live orders.",
    )
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    scan = sub.add_parser("scan", help="List liquid active markets (public data, read-only)")
    scan.add_argument("--source", choices=["live", "fixture", "replay"], default="live")
    scan.add_argument("--fixture", default=None)
    scan.add_argument("--json", action="store_true")
    scan.set_defaults(func=cmd_scan)

    paper = sub.add_parser("paper", help="Run a short paper-trading session")
    paper.add_argument("--source", choices=["live", "fixture", "replay"], default="replay")
    paper.add_argument("--fixture", default=None)
    paper.add_argument("--ticks", type=int, default=None)
    paper.add_argument("--no-sleep", action="store_true")
    paper.set_defaults(func=cmd_paper)

    report = sub.add_parser("report", help="Print the last paper-trading report")
    report.add_argument("--state", default=None)
    report.set_defaults(func=cmd_report)

    live = sub.add_parser("live", help="Disabled. Always raises.")
    live.set_defaults(func=cmd_live)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except LiveTradingDisabled as exc:
        print(str(exc), file=sys.stderr)
        return 2
