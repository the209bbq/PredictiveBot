import json
import threading
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx

from polymarket_bot.config import DashboardConfig, TradingConfig, load_config
from polymarket_bot.dashboard import PAGE, _normalize_fill, build_snapshot, make_handler
from polymarket_bot.pnl import append_pnl, history_path, load_history, snapshot_from_state, summarize
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
        "trading",
        TradingConfig(True, tmp_path / "toggle.json", tmp_path / "lock"),
    )
    object.__setattr__(
        cfg,
        "dashboard",
        DashboardConfig("127.0.0.1", 8787, 3, tmp_path / "pnl.jsonl"),
    )
    append_pnl(cfg.dashboard.pnl_path, json.loads(state_path.read_text()))
    snap = build_snapshot(cfg, include_exchange=False)
    assert snap["environment"] == "PAPER"
    assert snap["live_trading_enabled"] is False
    assert snap["exchange"]["quotes"] == {}
    assert snap["markets"][0]["slug"] == "KXDEMO-COIN"
    assert snap["exposure"]["pct_display"] == "12.00%"
    assert "daily_limits" in snap
    assert snap["daily_limits"]["max_daily_capital_in_use_usd"] == Decimal("100")
    assert snap["daily_limits"]["max_daily_loss_usd"] == Decimal("50")
    assert snap["pnl"]["all_time"]["net_pnl"] == Decimal("5.00")
    assert snap["pnl"]["all_time"]["realized"] == Decimal("2.00")
    assert "compare" not in snap


def test_dashboard_demo_env_uses_demo_state_only(tmp_path):
    cfg = load_config(environment="demo")
    paper = tmp_path / "paper_state.json"
    paper.write_text(json.dumps({"live": False, "demo": False, "trading": "on", "ticks": 9}))
    demo = tmp_path / "demo_state.json"
    demo.write_text(
        json.dumps(
            {
                "demo": True,
                "live": False,
                "running": True,
                "quotes_placed": 3,
                "ending_cash": "98.18",
                "fills": [{"ticker": "KXDEMO-COIN", "side": "yes", "count_fp": "2.00", "yes_price_dollars": "0.50"}],
                "maker": {"equity": "98.18", "cash": "98.18", "fills": []},
            }
        )
    )
    object.__setattr__(cfg.logging, "state_path", paper)
    object.__setattr__(cfg.logging, "demo_state_path", demo)
    snap = build_snapshot(cfg, include_exchange=False)
    assert snap["environment"] == "DEMO"
    assert snap["source"] == "demo"
    assert snap["fills"][0]["qty"] == Decimal("2.00")
    assert snap["fills"][0]["market"] == "KXDEMO-COIN"
    mapped = _normalize_fill({"ticker": "KXRT-FOO", "side": "yes", "count": 4, "yes_price_dollars": "0.40"})
    assert mapped["qty"] == Decimal("4")
    assert "count_fp" in PAGE or "f.qty||f.count_fp||f.count" in PAGE


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
        assert "switch" in page.text
        assert "LIVE TRADING DISABLED" in page.text
        assert "Capital in use" in page.text
        assert "daily loss limit hit" in page.text
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


def test_pnl_windows_are_deltas_and_live_file_is_separate(tmp_path):
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    rows = [
        snapshot_from_state(
            {"live": False, "demo": False, "maker": {"equity": "100", "cash": "100", "net_pnl": "1", "realized_pnl": "1", "rebates": "0.10", "positions": {}, "max_drawdown": "0.2"}},
            now=now - timedelta(days=10),
        ),
        snapshot_from_state(
            {"live": False, "demo": False, "maker": {"equity": "110", "cash": "110", "net_pnl": "4", "realized_pnl": "3", "rebates": "0.20", "positions": {}, "max_drawdown": "0.5"}},
            now=now - timedelta(days=3),
        ),
        snapshot_from_state(
            {"live": False, "demo": False, "maker": {"equity": "120", "cash": "120", "net_pnl": "7", "realized_pnl": "5", "rebates": "0.30", "positions": {}, "max_drawdown": "0.6"}},
            now=now,
        ),
    ]
    rolled = summarize(rows, now=now)
    assert rolled["all_time"]["net_pnl"] == Decimal("7")
    assert rolled["last_7d"]["net_pnl"] == Decimal("6")  # 7 - 1
    assert rolled["today"]["net_pnl"] == Decimal("3")  # 7 - 4
    assert rolled["today"]["realized"] == Decimal("2")
    assert rolled["today"]["fees"] == Decimal("0.10")
    assert len(rolled["equity_points"]) == 3

    base = tmp_path / "pnl.jsonl"
    append_pnl(base, {"live": True, "demo": False, "maker": {"equity": "1", "cash": "1", "net_pnl": "0", "realized_pnl": "0", "rebates": "0", "positions": {}, "max_drawdown": "0"}})
    assert history_path(base, "live").exists()
    assert not history_path(base, "demo").exists() or history_path(base, "demo").read_text() == ""
    assert "live" in history_path(base, "live").name
    assert "demo" not in history_path(base, "live").name
