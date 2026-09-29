from datetime import datetime, timezone
from decimal import Decimal

import pytest

from polymarket_bot.config import load_config
from polymarket_bot.exchanges.kalshi import snapshot_from_kalshi
from polymarket_bot.market_data import MarketSnapshot
from polymarket_bot.market_risk import (
    _dates_in_ids,
    DEFAULT_MARKET_RISK_SCORE,
    HARD_MAX_MARKET_RISK_SCORE,
    MarketRiskError,
    PriceHistory,
    attach_market_risk,
    compute_market_risk,
    format_score,
    is_live_in_game,
    is_news_driven,
    looks_like_game_market,
    looks_like_player_prop,
    market_over_risk_threshold,
    validate_market_risk_score,
)
from polymarket_bot.paper.maker import desired_quotes
from polymarket_bot.paper.near_resolution import desired_quotes as near_quotes
from polymarket_bot.paper.portfolio import Portfolio


def _snap(**overrides) -> MarketSnapshot:
    bid = Decimal(str(overrides.pop("bid", "0.49")))
    ask = Decimal(str(overrides.pop("ask", "0.51")))
    hours = overrides.pop("hours", 72)
    base = dict(
        slug=overrides.pop("slug", "KXOTHER-FAR"),
        question=overrides.pop("question", "Will it rain?"),
        category=overrides.pop("category", "weather"),
        status="open",
        end_date=None,
        hours_to_resolution=hours,
        best_bid=bid,
        best_ask=ask,
        mid=(bid + ask) / 2,
        spread=ask - bid,
        last_trade=Decimal(str(overrides.pop("last", "0.50"))),
        volume_shares=Decimal("1000"),
        open_interest=Decimal("1000"),
        notional_traded=None,
        bid_depth_contracts=Decimal(str(overrides.pop("bid_depth", "80"))),
        ask_depth_contracts=Decimal(str(overrides.pop("ask_depth", "80"))),
        tick_size=Decimal("0.01"),
        fee_coefficient=Decimal("0.07"),
        venue="kalshi",
        event_title=overrides.pop("event_title", None),
        raw=overrides.pop("raw", {}),
        stale=False,
        book_fetched=True,
    )
    base.update(overrides)
    return MarketSnapshot(**base)


def test_validate_hard_max_refused_without_override():
    with pytest.raises(MarketRiskError, match="hard maximum"):
        validate_market_risk_score(Decimal("0.51"), allow_above_hard_max=False)
    assert validate_market_risk_score(HARD_MAX_MARKET_RISK_SCORE, allow_above_hard_max=False) == Decimal(
        "0.50"
    )
    assert validate_market_risk_score(Decimal("0.60"), allow_above_hard_max=True) == Decimal("0.60")


def test_load_config_refuses_score_above_hard_max(tmp_path):
    path = tmp_path / "cfg.yaml"
    path.write_text(
        "dry_run: true\nlive_trading_enabled: false\n"
        "paper:\n  risk:\n    max_market_risk_score: 0.60\n"
    )
    with pytest.raises(MarketRiskError, match="hard maximum"):
        load_config(path)


def test_load_config_allows_score_override(tmp_path):
    path = tmp_path / "cfg.yaml"
    path.write_text(
        "dry_run: true\nlive_trading_enabled: false\n"
        "paper:\n  risk:\n    max_market_risk_score: 0.60\n"
        "    allow_market_risk_above_hard_max: true\n"
    )
    cfg = load_config(path)
    assert cfg.paper.risk.max_market_risk_score == Decimal("0.60")


def test_default_market_score_cap():
    cfg = load_config()
    assert cfg.paper.risk.max_market_risk_score == DEFAULT_MARKET_RISK_SCORE
    assert cfg.paper.risk.allow_market_risk_above_hard_max is False
    assert cfg.environment == "paper"


def test_demo_environment_uses_same_market_risk_cap():
    cfg = load_config(environment="demo")
    assert cfg.environment == "demo"
    assert cfg.paper.risk.max_market_risk_score == DEFAULT_MARKET_RISK_SCORE
    assert cfg.paper.risk.allow_market_risk_above_hard_max is False
    live = load_config(environment="live")
    assert live.paper.risk.max_market_risk_score == DEFAULT_MARKET_RISK_SCORE
    assert live.paper.risk.allow_market_risk_above_hard_max is False


def test_quote_size_default_and_warn(tmp_path, caplog):
    import logging

    cfg = load_config()
    assert cfg.paper.quote_size_contracts == Decimal("1")
    caplog.set_level(logging.WARNING, logger="polymarket_bot")
    path = tmp_path / "cfg.yaml"
    path.write_text("dry_run: true\nlive_trading_enabled: false\npaper:\n  quote_size_contracts: 10\n")
    load_config(path)
    assert any("quote_size_contracts" in rec.message for rec in caplog.records)


def test_live_nfl_game_scores_at_least_95():
    now = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)
    snap = snapshot_from_kalshi(
        {
            "ticker": "KXNFLGAME-26SEP28PHICHI-CHI",
            "title": "Eagles vs Bears",
            "event_title": "PHI vs CHI",
            "category": "Sports",
            "status": "active",
            "yes_bid_dollars": "0.49",
            "yes_ask_dollars": "0.51",
            "occurrence_datetime": "2026-09-28T20:15:00Z",
            "close_time": "2026-09-29T06:00:00Z",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.4900", "80"]],
                "no_dollars": [["0.4900", "80"]],
            }
        },
        now=now,
    )
    assert is_live_in_game(snap, now=now)
    score, parts = compute_market_risk(snap)
    assert parts["live_game"] == Decimal("1")
    assert score == Decimal("1")
    assert market_over_risk_threshold(snap, Decimal("0.40"))


def test_live_player_prop_on_started_game_is_100_and_not_quoted():
    now = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)
    snap = snapshot_from_kalshi(
        {
            "ticker": "KXNFLRECV-26SEP28PHICHI-COOPER",
            "title": "Cooper Kupp receiving yards",
            "yes_sub_title": "Cooper receiving yards O/U 64.5",
            "event_title": "PHI vs CHI",
            "event_ticker": "KXNFLGAME-26SEP28PHICHI",
            "series_ticker": "KXNFLRECV",
            "category": "Sports",
            "status": "active",
            "yes_bid_dollars": "0.49",
            "yes_ask_dollars": "0.51",
            "occurrence_datetime": "2026-09-28T20:15:00Z",
            "expected_expiration_time": "2026-09-29T04:00:00Z",
            "close_time": "2026-09-29T08:00:00Z",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.4900", "80"]],
                "no_dollars": [["0.4900", "80"]],
            }
        },
        now=now,
    )
    assert looks_like_player_prop(snap) or looks_like_game_market(snap)
    assert is_live_in_game(snap, now=now)
    score, parts = compute_market_risk(snap)
    assert parts["live_game"] == Decimal("1")
    assert score == Decimal("1")
    cfg = load_config()
    port = Portfolio("maker", Decimal("1000"), Decimal("1000"))
    assert desired_quotes(snap, port, cfg, "t0") == []


def test_kalshi_ticker_date_is_year_month_day():
    snap = snapshot_from_kalshi(
        {"ticker": "KXNFLGAME-26SEP28PHICHI-CHI", "title": "x", "status": "active"},
        None,
        now=datetime(2026, 9, 28, tzinfo=timezone.utc),
    )
    assert _dates_in_ids(snap) == [datetime(2026, 9, 28, tzinfo=timezone.utc).date()]


def test_cooper_prop_late_close_without_kickoff_is_live():
    """Repro: 4:58 PM PT during PHI-CHI. Close/expiration days later; no occurrence."""
    now = datetime(2026, 9, 28, 23, 58, tzinfo=timezone.utc)
    snap = snapshot_from_kalshi(
        {
            "ticker": "KXNFLYARDS-26SEP28PHICHI-COOPER",
            "title": "Cooper receiving yards",
            "yes_sub_title": "Over 64.5",
            "event_ticker": "KXNFLGAME-26SEP28PHICHI",
            "series_ticker": "KXNFLYARDS",
            "category": "Sports",
            "status": "active",
            "yes_bid_dollars": "0.48",
            "yes_ask_dollars": "0.50",
            "expected_expiration_time": "2026-09-30T12:00:00Z",
            "close_time": "2026-10-02T00:00:00Z",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.4800", "80"]],
                "no_dollars": [["0.5000", "80"]],
            }
        },
        now=now,
    )
    assert snap.hours_to_resolution is not None
    assert snap.hours_to_resolution > 6
    assert snap.hours_to_resolution > 24
    assert looks_like_player_prop(snap)
    assert is_live_in_game(snap, now=now)
    score, parts = compute_market_risk(snap, now=now)
    assert parts["live_game"] == Decimal("1")
    assert score == Decimal("1")
    cfg = load_config()
    port = Portfolio("maker", Decimal("1000"), Decimal("1000"))
    attach_market_risk(snap, now=now)
    assert desired_quotes(snap, port, cfg, "t0") == []


def test_pregame_with_future_kickoff_is_not_live_yet():
    now = datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc)
    snap = snapshot_from_kalshi(
        {
            "ticker": "KXNFLGAME-26SEP28PHICHI-CHI",
            "title": "Eagles vs Bears",
            "category": "Sports",
            "status": "active",
            "yes_bid_dollars": "0.49",
            "yes_ask_dollars": "0.51",
            "occurrence_datetime": "2026-09-28T20:15:00Z",
            "close_time": "2026-10-02T00:00:00Z",
        },
        None,
        now=now,
    )
    assert not is_live_in_game(snap, now=now)


def test_season_long_receiving_yards_not_live():
    now = datetime(2026, 9, 28, 23, 58, tzinfo=timezone.utc)
    snap = snapshot_from_kalshi(
        {
            "ticker": "KXNFLSEASON-COOPER-YDS",
            "title": "Cooper regular-season receiving yards",
            "category": "Sports",
            "status": "active",
            "yes_bid_dollars": "0.49",
            "yes_ask_dollars": "0.51",
            "close_time": "2027-02-01T00:00:00Z",
        },
        None,
        now=now,
    )
    assert looks_like_player_prop(snap)
    assert snap.hours_to_resolution is not None and snap.hours_to_resolution > 24
    assert not is_live_in_game(snap, now=now)


def test_same_day_game_is_live_even_if_close_is_far():
    snap = _snap(
        slug="KXNFLGAME-26SEP28PHICHI-CHI",
        question="PHI vs CHI",
        category="nfl",
        hours=10,
    )
    assert is_live_in_game(snap)
    cfg = load_config()
    port = Portfolio("maker", Decimal("1000"), Decimal("1000"))
    assert desired_quotes(snap, port, cfg, "t0") == []


def test_volatility_and_jump_raise_score():
    calm = _snap(slug="calm", hours=72, bid="0.20", ask="0.21", last="0.20", bid_depth="400", ask_depth="400")
    hist = PriceHistory(window=8)
    for mid in (Decimal("0.40"), Decimal("0.55"), Decimal("0.70"), Decimal("0.40")):
        hist.observe("jumpy", mid)
    jumpy = _snap(slug="jumpy", hours=72, bid="0.39", ask="0.41", last="0.70", bid_depth="400", ask_depth="400")
    calm_score, _ = compute_market_risk(calm, PriceHistory())
    jumpy_score, parts = compute_market_risk(jumpy, hist)
    assert parts["volatility"] > 0
    assert parts["jump"] > 0
    assert jumpy_score > calm_score


def test_news_coin_flip_stronger_than_non_news():
    news = _snap(slug="fed-rate-cut", question="Fed rate cut this week?", hours=72, bid="0.49", ask="0.51")
    other = _snap(slug="weather-rain", question="Will it rain?", hours=72, bid="0.49", ask="0.51")
    assert is_news_driven(news)
    assert not is_news_driven(other)
    news_score, news_parts = compute_market_risk(news)
    other_score, other_parts = compute_market_risk(other)
    assert news_parts["coin_flip"] > other_parts["coin_flip"]
    assert news_score > other_score


def test_components_normalized_and_format():
    snap = _snap()
    attach_market_risk(snap)
    assert snap.risk_score is not None
    assert Decimal("0") <= snap.risk_score <= Decimal("1")
    for key in ("volatility", "jump", "spread", "thin_book", "urgency", "coin_flip", "live_game"):
        assert key in snap.risk_components
        assert Decimal("0") <= snap.risk_components[key] <= Decimal("1")
    assert format_score(Decimal("0.40")) == "40.0%"


def test_maker_and_near_skip_over_threshold():
    cfg = load_config()
    object.__setattr__(cfg.paper.risk, "max_market_risk_score", Decimal("0.10"))
    port = Portfolio("maker", Decimal("1000"), Decimal("1000"))
    live = _snap(slug="KXNFLGAME-X", question="DAL vs NYG", category="nfl", hours=8)
    assert desired_quotes(live, port, cfg, "t0") == []
    favorite = _snap(slug="fav", bid="0.96", ask="0.97", hours=12)
    attach_market_risk(favorite)
    favorite.risk_score = Decimal("0.90")
    assert near_quotes(favorite, port, cfg, "t0") == []
