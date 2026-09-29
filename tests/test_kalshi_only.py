import pytest

from polymarket_bot.cli import main
from polymarket_bot.config import load_config
from polymarket_bot.dashboard import PAGE
from polymarket_bot.exchanges.factory import build_client, compare_is_enabled


def test_defaults_are_kalshi_only():
    cfg = load_config()
    assert cfg.exchange == "kalshi"
    assert cfg.polymarket_us_enabled is False
    assert cfg.compare.enabled is False
    assert compare_is_enabled(cfg) is False


def test_build_client_refuses_polymarket_by_default():
    cfg = load_config()
    with pytest.raises(SystemExit, match="disabled"):
        build_client(cfg, source="live", exchange="polymarket_us")
    with pytest.raises(SystemExit, match="disabled"):
        build_client(cfg, source="fixture", exchange="polymarket_us")


def test_compare_cli_refuses_when_disabled(capsys):
    assert main(["compare"]) == 2
    err = capsys.readouterr().err
    assert "disabled" in err.lower()
    assert "Kalshi only" in err


def test_dashboard_page_has_no_compare_section():
    assert "<h2>Compare</h2>" not in PAGE
    assert "id=\"compare\"" not in PAGE


def test_recorder_client_is_public_only_no_orders():
    from polymarket_bot.guard import DemoOrderError

    cfg = load_config()
    assert cfg.record.data_host == "production"
    client = build_client(cfg, source="live", exchange="kalshi", public_only=True)
    assert client.public_only is True
    assert "external-api.kalshi.com" in client.data_base_url or "kalshi.com" in client.data_base_url
    with pytest.raises(DemoOrderError, match="public-data only"):
        client.signed_demo("GET", "/portfolio/balance", confirm_demo=True)


def test_no_forbidden_feeds_or_scrapers():
    from pathlib import Path

    banned = (
        "weather.com",
        "rottentomatoes.com",
        "metacritic.com",
        "truthsocial.com",
        "whitehouse.gov",
    )
    root = Path("polymarket_bot")
    for path in root.rglob("*.py"):
        text = path.read_text().lower()
        for token in banned:
            assert token not in text, f"{path} mentions {token}"
    cfg = load_config()
    disabled = [str(x).upper() for x in ((cfg.extra.get("paper") or {}).get("series") or {}).get("disabled") or []]
    assert "KXTRUTHSOCIAL" in disabled
    assert "KXTRUTHSOCIAL" not in [str(x).upper() for x in ((cfg.extra.get("paper") or {}).get("series") or {}).get("allow") or []]
    from polymarket_bot.exchanges.kalshi import snapshot_from_kalshi
    from polymarket_bot.series_filter import maker_universe_ok

    truth = snapshot_from_kalshi(
        {
            "ticker": "KXTRUTHSOCIAL-X",
            "series_ticker": "KXTRUTHSOCIAL",
            "title": "Truth Social",
            "status": "active",
            "yes_bid_dollars": "0.49",
            "yes_ask_dollars": "0.51",
            "close_time": "2027-01-01T00:00:00Z",
            "fee_type": "quadratic",
        },
        None,
    )
    cfg.extra["paper"]["series"]["allow"].append("KXTRUTHSOCIAL")
    assert not maker_universe_ok(truth, cfg)


def test_load_config_isolates_repo_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PMBOT_STATE_DIR", str(tmp_path / "isolated"))
    cfg = load_config()
    assert str(tmp_path / "isolated") in str(cfg.trading.toggle_path)
    assert "state/trading_toggle.json" not in str(cfg.trading.toggle_path)
