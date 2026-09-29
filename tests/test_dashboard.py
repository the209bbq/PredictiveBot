import json
import threading
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx

from polymarket_bot.config import DashboardConfig, TradingConfig, load_config
from polymarket_bot.dashboard import build_snapshot, make_handler
from polymarket_bot.pnl import append_pnl, history_path, load_history, summarize
from polymarket_bot.trading import read_toggle


def test_build_snapshot_includes_risk_and_pnl(tmp_path):
    cfg = load_config()
    state_path = tmp_path / "paper_state.json"
    state_path.write_text(
        json.dumps(
            {
                "live": False,
                "demo": False,
                "trading": "on",
                "ticks": 2,
                "account_risk_cap": "40.00%",
                "market_risk_cap": "40.0%",
                "market_risk": {
                    "KXDEMO-COIN": {
                        "score": "0.23",
                        "score_display": "23.0%",
                        "components": {"volatility": "0.1", "live_game": "0"},
                    }
                },
                "maker": {
                    "equity": "1005.00",
                    "cash": "990.00",
                    "net_pnl": "5.00",
                    "realized_pnl": "2.00",
                    "rebates": "0.10",
                    "max_drawdown": "0.50",
                    "fill_count": 2,
                    "positions": {"KXDEMO-COIN": {"qty": "10", "avg_price": "0.50", "realized_pnl": "2.00"}},
                    "account_risk": {"pct_display": "12.00%", "cap_display": "40.00%"},
                },
                "near_resolution": {},
            }
        )
    )
    object.__setattr__(cfg.logging, "state_path", state_path)
    object.__setattr__(
        cfg,
        "dashboard",
        DashboardConfig("127.0.0.1", 8787, 3, tmp_path / "pnl.jsonl"),
    )
    append_pnl(cfg.dashboard.pnl_path, json.loads(state_path.read_text()))
    snap = build_snapshot(cfg)
    assert snap["environment"] == "PAPER"
    assert snap["live_trading_enabled"] is False
    assert snap["markets"][0]["slug"] == "KXDEMO-COIN"
    assert snap["exposure"]["pct_display"] == "12.00%"
    assert snap["pnl"]["all_time"]["net_pnl"] == Decimal("5.00")
    assert snap["pnl"]["all_time"]["realized"] == Decimal("2.00")


def test_dashboard_http_toggle_and_auth(tmp_path, monkeypatch):
    cfg = load_config()
    object.__setattr__(
        cfg,
        "trading",
        TradingConfig(True, tmp_path / "toggle.json", tmp_path / "lock"),
    )
    object.__setattr__(
        cfg,
        "dashboard",
        DashboardConfig("127.0.0.1", 0, 3, tmp_path / "pnl.jsonl"),
    )
    (tmp_path / "paper_state.json").write_text("{}")
    object.__setattr__(cfg.logging, "state_path", tmp_path / "paper_state.json")
    handler = make_handler(cfg)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        page = httpx.get(f"{base}/", timeout=3)
        assert page.status_code == 200
        assert "Trading" in page.text
        snap = httpx.get(f"{base}/api/snapshot", timeout=3).json()
        assert snap["environment"] in {"PAPER", "DEMO"}
        bad = httpx.post(f"{base}/api/trading", json={"enabled": False}, timeout=3)
        assert bad.status_code == 400
        ok = httpx.post(
            f"{base}/api/trading",
            json={"enabled": False, "confirm": True},
            timeout=3,
        )
        assert ok.status_code == 200
        assert read_toggle(cfg.trading.toggle_path) is False
    finally:
        server.shutdown()

    object.__setattr__(
        cfg,
        "dashboard",
        DashboardConfig("0.0.0.0", 0, 3, tmp_path / "pnl.jsonl"),
    )
    monkeypatch.setenv("DASHBOARD_TOKEN", "secret")
    handler = make_handler(cfg)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        denied = httpx.get(f"{base}/api/snapshot", timeout=3)
        assert denied.status_code == 401
        allowed = httpx.get(
            f"{base}/api/snapshot",
            headers={"Authorization": "Bearer secret"},
            timeout=3,
        )
        assert allowed.status_code == 200
    finally:
        server.shutdown()


def test_pnl_history_keeps_demo_and_paper_separate(tmp_path):
    base = tmp_path / "pnl.jsonl"
    paper = {"live": False, "demo": False, "maker": {"equity": "10", "cash": "10", "net_pnl": "1", "realized_pnl": "1", "rebates": "0", "positions": {}, "max_drawdown": "0"}}
    demo = {"live": False, "demo": True, "ending_cash": "20", "starting_cash": "10", "maker": {"equity": "20", "cash": "20", "net_pnl": "10", "realized_pnl": "4", "rebates": "-1", "positions": {"X": {"qty": "1", "avg_price": "0.4", "realized_pnl": "4"}}, "max_drawdown": "2"}}
    append_pnl(base, paper)
    append_pnl(base, demo)
    assert history_path(base, "paper").exists()
    assert history_path(base, "demo").exists()
    assert not history_path(base, "live").exists()
    paper_rows = load_history(base, "paper")
    demo_rows = load_history(base, "demo")
    assert len(paper_rows) == 1
    assert len(demo_rows) == 1
    rolled = summarize(demo_rows)
    assert rolled["all_time"]["net_pnl"] == Decimal("10")
    assert rolled["all_time"]["max_drawdown"] == Decimal("2")
    assert "X" in rolled["markets"]
