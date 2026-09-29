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
