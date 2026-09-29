import json
from datetime import datetime, timezone

import pytest

from polymarket_bot.cli import main
from polymarket_bot.config import TradingConfig, load_config
from polymarket_bot.logging_utils import DecisionLogger
from polymarket_bot.market_data.replay_client import ReplayClient
from polymarket_bot.paper.engine import run_paper
from polymarket_bot.trading import (
    TradingLock,
    TradingLockHeld,
    read_toggle,
    set_trading_enabled,
    trading_is_on,
    trading_status,
)


def test_toggle_file_roundtrip(tmp_path):
    path = tmp_path / "toggle.json"
    assert read_toggle(path) is None
    set_trading_enabled(path, False)
    assert read_toggle(path) is False
    set_trading_enabled(path, True)
    assert read_toggle(path) is True
    payload = json.loads(path.read_text())
    assert payload["enabled"] is True
    assert "updated_at" in payload


def _with_trading(cfg, tmp_path, *, enabled: bool = True):
    object.__setattr__(
        cfg,
        "trading",
        TradingConfig(
            enabled=enabled,
            toggle_path=tmp_path / "toggle.json",
            lock_path=tmp_path / "lock",
        ),
    )
    return cfg


def test_trading_is_on_ands_config_and_file(tmp_path):
    cfg = _with_trading(load_config(), tmp_path)
    path = cfg.trading.toggle_path
    assert trading_is_on(cfg) == (True, "on")
    set_trading_enabled(path, False)
    assert trading_is_on(cfg) == (False, "toggle_off")
    set_trading_enabled(path, True)
    assert trading_is_on(cfg) == (True, "on")
    _with_trading(cfg, tmp_path, enabled=False)
    assert trading_is_on(cfg) == (False, "config_disabled")


def test_toggle_reread_every_call(tmp_path):
    cfg = _with_trading(load_config(), tmp_path)
    assert trading_is_on(cfg)[0] is True
    set_trading_enabled(cfg.trading.toggle_path, False)
    assert trading_is_on(cfg)[0] is False


def test_lock_refuses_second_instance(tmp_path):
    path = tmp_path / "trading.lock"
    first = TradingLock(path)
    first.acquire()
    try:
        with pytest.raises(TradingLockHeld, match="already running"):
            TradingLock(path).acquire()
    finally:
        first.release()
    second = TradingLock(path)
    second.acquire()
    second.release()


def test_paper_pauses_when_toggle_off(tmp_path):
    cfg = _with_trading(load_config(), tmp_path)
    set_trading_enabled(cfg.trading.toggle_path, False)
    now = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)
    client = ReplayClient("fixtures/replay.json", now=now)
    logger = DecisionLogger(tmp_path / "decisions.jsonl")
    try:
        state = run_paper(client, cfg, logger, ticks=2, sleep=False, now=now)
    finally:
        logger.close()
        client.close()
    text = (tmp_path / "decisions.jsonl").read_text()
    assert "trading_paused" in text
    assert '"action": "quote"' not in text
    assert int(state["maker"]["fill_count"]) == 0
    assert state["trading"] == "off"
    assert "account_risk" in (state["maker"])


def test_paper_second_process_refuses_lock(tmp_path):
    cfg = _with_trading(load_config(), tmp_path)
    held = TradingLock(cfg.trading.lock_path)
    held.acquire()
    try:
        now = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)
        client = ReplayClient("fixtures/replay.json", now=now)
        logger = DecisionLogger(tmp_path / "d.jsonl")
        try:
            with pytest.raises(TradingLockHeld):
                run_paper(client, cfg, logger, ticks=1, sleep=False, now=now)
        finally:
            logger.close()
            client.close()
    finally:
        held.release()


def test_trading_cli_status_on_off(tmp_path, monkeypatch, capsys):
    cfg_path = tmp_path / "cfg.yaml"
    toggle = tmp_path / "toggle.json"
    cfg_path.write_text(
        "dry_run: true\nlive_trading_enabled: false\n"
        f"trading:\n  enabled: true\n  toggle_path: {toggle}\n"
        f"  lock_path: {tmp_path / 'lock'}\n"
    )
    assert main(["--config", str(cfg_path), "trading", "off"]) == 0
    assert read_toggle(toggle) is False
    assert main(["--config", str(cfg_path), "trading", "status"]) == 0
    out = capsys.readouterr().out
    assert "Trading: off" in out
    assert main(["--config", str(cfg_path), "trading", "on"]) == 0
    assert read_toggle(toggle) is True
    assert trading_status(load_config(cfg_path))["effective"] == "on"
