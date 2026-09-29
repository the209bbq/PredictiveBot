from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from polymarket_bot.config import load_config
from polymarket_bot.dashboard import PAGE, build_snapshot
from polymarket_bot.exchanges.kalshi import snapshot_from_kalshi
from polymarket_bot.favorites import (
    FILL_TAG,
    STRATEGY,
    desired_quotes,
    favorite_side,
    favorites_settings,
    favorites_universe_ok,
    fill_row_from_order,
    format_favorites_report,
    hard_excluded,
    in_entry_window,
    is_favorites_mode,
    favorites_listed_ok,
    load_fill_rows,
    log_favorites_fill,
    reconcile_fills,
    summarize_fills,
    wilson_interval,
    would_cross,
    write_csv,
)
from polymarket_bot.cli import main
from polymarket_bot.logging_utils import DecisionLogger
from polymarket_bot.market_risk import attach_market_risk
from polymarket_bot.paper.engine import run_paper
from polymarket_bot.paper.fills import fill_qty
from polymarket_bot.paper.portfolio import PaperOrder, Portfolio
from polymarket_bot.series_filter import listed_market_ok as series_listed_ok
from polymarket_bot.series_filter import maker_min_hours, maker_universe_ok


NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def _fav_cfg():
    cfg = load_config()
    cfg.extra.setdefault("paper", {})["strategy"] = "favorites_maker"
    return cfg


def _weather(ticker="KXHIGHNY-29SEP26", **fields):
    now = fields.pop("now", NOW)
    bid = fields.pop("bid", "0.1400")
    ask = fields.pop("ask", "0.1600")
    close = fields.pop("close", "2026-09-29T15:00:00Z")
    depth = fields.pop("depth", "12")
    no_bid = f"{(Decimal('1') - Decimal(ask)):.4f}"
    market = {
        "ticker": ticker,
        "slug": ticker,
        "series_ticker": fields.pop("series", ticker.split("-", 1)[0]),
        "title": fields.pop("title", "NYC high temp"),
        "category": fields.pop("category", "Climate and Weather"),
        "status": "active",
        "yes_bid_dollars": bid,
        "yes_ask_dollars": ask,
        "close_time": close,
        "fee_type": fields.pop("fee_type", "quadratic"),
    }
    book = {
        "orderbook_fp": {
            "yes_dollars": [[f"{Decimal(bid):.4f}", depth]],
            "no_dollars": [[no_bid, depth]],
        }
    }
    snap = snapshot_from_kalshi(market, book, now=now)
    snap.stale = False
    snap.book_fetched = True
    return snap


def test_demo_defaults_to_favorites_maker_and_drops_payrolls():
    paper = load_config()
    demo = load_config(environment="demo")
    assert is_favorites_mode(paper) is False
    assert is_favorites_mode(demo) is True
    settings = favorites_settings(demo)
    assert settings.sides == ("no",)
    assert settings.allow_yes is False
    assert settings.min_price == Decimal("0.80")
    assert settings.max_price == Decimal("0.97")
    assert settings.max_hours_to_close == 6.0
    assert settings.min_hours_to_close == 0.25
    assert settings.late_entry_enabled is True
    assert settings.fill_tag == FILL_TAG
    assert settings.rescan_minutes == 10.0
    assert settings.max_markets == 5
    assert paper.paper.risk.kxhigh_resolution_day_enabled is False
    assert demo.paper.risk.kxhigh_resolution_day_enabled is False
    demo_allow = [str(x).upper() for x in ((demo.extra.get("paper") or {}).get("series") or {}).get("allow") or []]
    paper_allow = [str(x).upper() for x in ((paper.extra.get("paper") or {}).get("series") or {}).get("allow") or []]
    assert demo_allow == ["KXHIGH", "KXRAIN"]
    assert "KXPAYROLLS" not in demo_allow
    assert "KXPAYROLLS" in paper_allow
    assert paper.paper.risk.max_daily_capital_in_use_usd == Decimal("100")
    assert paper.paper.risk.max_daily_loss_usd == Decimal("50")
    assert paper.paper.risk.max_account_risk_pct == Decimal("0.40")
    assert paper.paper.risk.max_market_risk_score == Decimal("0.40")
    assert demo.paper.risk.max_market_risk_score == Decimal("0.40")


def test_no_only_default_skips_yes_favorites():
    cfg = _fav_cfg()
    settings = favorites_settings(cfg)
    no_fav = _weather()
    yes_fav = _weather(bid="0.8500", ask="0.8700")
    assert favorite_side(no_fav, settings) == "no"
    assert favorite_side(yes_fav, settings) is None
    port = Portfolio("fav", Decimal("1000"), Decimal("1000"))
    assert desired_quotes(no_fav, port, cfg, "t0", now=NOW)
    assert desired_quotes(yes_fav, port, cfg, "t0", now=NOW) == []
    cfg.extra["paper"]["favorites"] = dict(cfg.extra["paper"].get("favorites") or {})
    cfg.extra["paper"]["favorites"]["allow_yes"] = True
    cfg.extra["paper"]["favorites"]["sides"] = ["no", "yes"]
    assert favorite_side(yes_fav, favorites_settings(cfg)) == "yes"


def test_never_cross_post_only_join_or_improve_one_cent():
    cfg = _fav_cfg()
    port = Portfolio("fav", Decimal("1000"), Decimal("1000"))
    # NO bid 0.84 / ask 0.86 → improve 1c to 0.85, still below ask.
    wide = _weather(bid="0.1400", ask="0.1600")
    quotes = desired_quotes(wide, port, cfg, "t0", now=NOW)
    assert len(quotes) == 1
    assert quotes[0].side == "buy"
    assert quotes[0].contract_side == "no"
    assert quotes[0].price == Decimal("0.85")
    assert quotes[0].fill_tag == FILL_TAG
    assert not would_cross(quotes[0].price, Decimal("0.86"))
    # 1-tick NO book: bid 0.84 ask 0.85. Improve would lock/cross — join 0.84.
    tight = _weather(bid="0.1500", ask="0.1600")
    quotes = desired_quotes(tight, port, cfg, "t0", now=NOW)
    assert quotes[0].price == Decimal("0.84")
    assert not would_cross(quotes[0].price, Decimal("0.85"))
    # Never submit price >= ask even if improve wants it.
    locked = _weather(bid="0.1400", ask="0.1500")
    for order in desired_quotes(locked, port, cfg, "t0", now=NOW):
        assert order.price < Decimal("0.86")
        assert not would_cross(order.price, Decimal("0.86"))


def test_exclusions_even_if_allowlisted():
    cfg = _fav_cfg()
    cfg.extra["paper"]["favorites"] = dict(cfg.extra["paper"].get("favorites") or {})
    cfg.extra["paper"]["favorites"]["allow"] = ["KXHIGH", "KXRAIN", "KXPAYROLLS", "KXU3", "KXINX", "KXNFL"]
    payroll = _weather("KXPAYROLLS-OCT", series="KXPAYROLLS", category="Economics", bid="0.1400", ask="0.1600")
    u3 = _weather("KXU3-OCT", series="KXU3", category="Economics")
    cpi = _weather("KXCPI-OCT", series="KXCPI", category="Economics")
    spx = _weather("KXINX-SPX", series="KXINX", category="Financials", title="S&P 500")
    sport = _weather("KXNFLGAME-X", series="KXNFLGAME", category="Sports", title="Eagles vs Bears")
    truth = _weather("KXTRUTHSOCIAL-X", series="KXTRUTHSOCIAL", title="Truth Social")
    rain = _weather("KXRAINNY-29SEP26", series="KXRAIN", category="Climate and Weather")
    assert hard_excluded(payroll) is True
    assert hard_excluded(u3) is True
    assert hard_excluded(cpi) is True
    assert hard_excluded(spx) is True
    assert hard_excluded(sport) is True
    assert hard_excluded(truth) is True
    assert hard_excluded(rain) is False
    assert not favorites_universe_ok(payroll, cfg, now=NOW)
    assert not favorites_universe_ok(sport, cfg, now=NOW)
    assert not series_listed_ok(
        {"ticker": "KXPAYROLLS-OCT", "series_ticker": "KXPAYROLLS", "category": "Economics", "fee_type": "quadratic"},
        cfg,
        now=NOW,
    )
    assert favorites_universe_ok(rain, cfg, now=NOW)
    assert favorites_universe_ok(_weather(), cfg, now=NOW)


def test_entry_window_and_late_flag():
    cfg = _fav_cfg()
    settings = favorites_settings(cfg)
    at_6h = _weather(close="2026-09-29T18:00:00Z")
    at_7h = _weather(close="2026-09-29T19:00:00Z")
    at_15m = _weather(close="2026-09-29T12:15:00Z")
    at_10m = _weather(close="2026-09-29T12:10:00Z")
    assert in_entry_window(at_6h, settings) is True
    assert in_entry_window(at_7h, settings) is False
    assert in_entry_window(at_15m, settings) is True
    assert in_entry_window(at_10m, settings) is False
    assert favorites_universe_ok(at_6h, cfg, now=NOW)
    assert not favorites_universe_ok(at_7h, cfg, now=NOW)
    assert not favorites_universe_ok(at_10m, cfg, now=NOW)
    two_sided = load_config()
    assert two_sided.paper.risk.kxhigh_resolution_day_enabled is False
    assert not maker_universe_ok(at_6h, two_sided)
    assert maker_min_hours(at_6h, cfg) == 0.25
    cfg.extra["paper"]["favorites"] = dict(cfg.extra["paper"].get("favorites") or {})
    cfg.extra["paper"]["favorites"]["late_entry_enabled"] = False
    assert in_entry_window(at_6h, favorites_settings(cfg)) is False


def test_fee_type_skips_maker_fee_series():
    cfg = _fav_cfg()
    charged = _weather(fee_type="quadratic_with_maker_fees")
    assert not favorites_universe_ok(charged, cfg, now=NOW)
    assert not favorites_listed_ok(
        {
            "ticker": "KXHIGHNY-29SEP26",
            "series_ticker": "KXHIGH",
            "fee_type": "quadratic_with_maker_fees",
            "close_time": "2026-09-29T15:00:00Z",
            "yes_bid_dollars": "0.14",
            "yes_ask_dollars": "0.16",
            "category": "Climate and Weather",
        },
        cfg,
        now=NOW,
    )
    cfg.extra["paper"]["favorites"] = dict(cfg.extra["paper"].get("favorites") or {})
    cfg.extra["paper"]["favorites"]["allow_maker_fee_series"] = True
    assert favorites_universe_ok(charged, cfg, now=NOW)


def test_existing_limits_still_gate_favorites():
    cfg = _fav_cfg()
    snap = _weather()
    broke = Portfolio("fav", Decimal("0"), Decimal("0"))
    assert desired_quotes(snap, broke, cfg, "t0", now=NOW) == []
    port = Portfolio("fav", Decimal("1000"), Decimal("1000"))
    port.position(snap.slug).qty = Decimal("5")
    assert desired_quotes(snap, port, cfg, "t0", now=NOW) == []
    port = Portfolio("fav", Decimal("1000"), Decimal("1000"))
    object.__setattr__(cfg.paper.risk, "max_account_risk_pct", Decimal("0.0001"))
    assert desired_quotes(snap, port, cfg, "t0", now=NOW) == []
    object.__setattr__(cfg.paper.risk, "max_account_risk_pct", Decimal("0.40"))
    object.__setattr__(cfg.paper.risk, "max_daily_capital_in_use_usd", Decimal("0.10"))
    assert desired_quotes(snap, port, cfg, "t0", now=NOW) == []
    object.__setattr__(cfg.paper.risk, "max_daily_capital_in_use_usd", Decimal("100"))
    object.__setattr__(cfg.paper.risk, "max_market_risk_score", Decimal("0.01"))
    attach_market_risk(snap)
    assert desired_quotes(snap, port, cfg, "t0", now=NOW) == []


def test_risk_score_does_not_mechanically_block_late_80c_weather():
    six_h = _weather(close="2026-09-29T18:00:00Z", depth="12")
    fifteen = _weather(close="2026-09-29T12:15:00Z", depth="12")
    attach_market_risk(six_h)
    attach_market_risk(fifteen)
    assert six_h.risk_score is not None
    assert fifteen.risk_score is not None
    assert six_h.risk_score < Decimal("0.40"), f"6h 80c weather scored {six_h.risk_score} (>= 0.40 cap)"
    assert fifteen.risk_score < Decimal("0.40"), f"15m 80c weather scored {fifteen.risk_score} (>= 0.40 cap)"
    cfg = _fav_cfg()
    port = Portfolio("fav", Decimal("1000"), Decimal("1000"))
    assert desired_quotes(six_h, port, cfg, "t0", now=NOW)
    assert desired_quotes(fifteen, port, cfg, "t0", now=NOW)


def test_fill_log_reconcile_and_report_math(tmp_path):
    cfg = _fav_cfg()
    snap = _weather()
    order = PaperOrder(
        "o1",
        snap.slug,
        "buy",
        Decimal("0.84"),
        Decimal("2"),
        STRATEGY,
        venue="kalshi",
        contract_side="no",
        fill_tag=FILL_TAG,
    )
    row = fill_row_from_order(
        order,
        snap=snap,
        filled_qty=Decimal("2"),
        filled_at=NOW,
        settings=favorites_settings(cfg),
    )
    assert row["side"] == "no"
    assert row["fill_tag"] == FILL_TAG
    assert row["hours_to_close"] == snap.hours_to_resolution
    assert log_favorites_fill(cfg, row)
    logged = load_fill_rows(favorites_settings(cfg).fills_path)
    assert len(logged) == 1
    settled = reconcile_fills(
        logged,
        now=NOW + timedelta(hours=8),
        markets={snap.slug: {"status": "settled", "result": "no"}},
        mids_at={snap.slug: {"mid_1m": Decimal("0.83"), "mid_5m": Decimal("0.82"), "mid_30m": Decimal("0.81")}},
    )
    assert settled[0]["settled"] is True
    assert settled[0]["outcome"] == "no"
    assert Decimal(str(settled[0]["pnl_after_fees"])) == Decimal("0.32")  # (1-0.84)*2
    lose = dict(row)
    lose["ticker"] = "KXHIGHNY-LOSE"
    lose["price"] = "0.90"
    lose["contracts"] = "1"
    lose["maker_fee"] = "0"
    lose_row = reconcile_fills(
        [lose],
        markets={"KXHIGHNY-LOSE": {"status": "settled", "result": "yes"}},
    )[0]
    summary = summarize_fills(settled + [lose_row])
    assert summary["n_settled"] == 2
    assert summary["wins"] == 1
    assert summary["win_rate"] == 0.5
    assert summary["pnl_after_fees"] == Decimal("0.32") + Decimal("-0.90")
    assert summary["pnl_per_contract"] == (Decimal("0.32") - Decimal("0.90")) / Decimal("3")
    lo, hi = wilson_interval(1, 2)
    assert 0.0 <= lo <= 0.5 <= hi <= 1.0
    lo8, hi8 = wilson_interval(8, 10)
    assert abs(lo8 - 0.490) < 0.02
    assert abs(hi8 - 0.943) < 0.02
    text = format_favorites_report(summary)
    assert "N fills" in text
    assert "Wilson" in text
    csv_path = tmp_path / "fav.csv"
    write_csv(settled, csv_path)
    assert "ticker" in csv_path.read_text()


def test_no_side_fill_uses_yes_tape_through():
    snap = _weather()
    order = PaperOrder(
        "o1",
        snap.slug,
        "buy",
        Decimal("0.84"),
        Decimal("1"),
        STRATEGY,
        contract_side="no",
        fill_tag=FILL_TAG,
    )
    from polymarket_bot.market_data import TapeTrade

    snap.tape = [TapeTrade(price=Decimal("0.20"), qty=Decimal("2"))]
    qty, reason = fill_qty(order, snap, None, strict=True)
    assert reason == "trade_through"
    assert qty == Decimal("1")


def test_paper_engine_logs_favorites_fill(tmp_path):
    cfg = _fav_cfg()
    snap = _weather()
    from polymarket_bot.market_data import TapeTrade

    class _Fake:
        source_name = "fav-fake"
        venue = "kalshi"

        def list_markets(self, **_k):
            return [snap.raw.get("market") or {"ticker": snap.slug}]

        def book(self, slug):
            return {
                "orderbook_fp": {
                    "yes_dollars": [["0.1400", "12"]],
                    "no_dollars": [["0.8400", "12"]],
                }
            }

        def snapshot(self, market, book=None, *, now=None):
            now = now or NOW
            payload = dict(market) if isinstance(market, dict) else dict(snap.raw.get("market") or {})
            payload["close_time"] = (now + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
            payload.setdefault("ticker", snap.slug)
            payload.setdefault("series_ticker", "KXHIGH")
            payload.setdefault("fee_type", "quadratic")
            payload.setdefault("category", "Climate and Weather")
            out = snapshot_from_kalshi(payload, book, now=now)
            out.stale = False
            out.book_fetched = True
            out.tape = [TapeTrade(price=Decimal("0.20"), qty=Decimal("3"))]
            return out

        def last_trade(self, slug):
            return Decimal("0.20")

        def close(self):
            return None

    logger = DecisionLogger(tmp_path / "decisions.jsonl")
    try:
        state = run_paper(_Fake(), cfg, logger, ticks=2, sleep=False, now=NOW, markets=[snap])
    finally:
        logger.close()
    assert state["strategy"] == STRATEGY
    rows = load_fill_rows(favorites_settings(cfg).fills_path)
    assert rows
    assert rows[0]["fill_tag"] == FILL_TAG
    assert rows[0]["side"] == "no"


def test_favorites_report_cli_and_dashboard_panel(tmp_path, capsys):
    cfg = _fav_cfg()
    path = favorites_settings(cfg).fills_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"ticker":"KXHIGHNY-X","series":"KXHIGH","side":"no","price":"0.84","contracts":"1",'
        '"maker_fee":"0","ts_filled":"2026-09-29T12:00:00+00:00","hours_to_close":3,'
        '"mid":"0.85","mid_1m":"0.84","settled":true,"outcome":"no","pnl_after_fees":"0.16",'
        '"fill_tag":"favorites_late"}\n'
    )
    csv_path = tmp_path / "out.csv"
    assert main(["favorites-report", "--fills", str(path), "--csv", str(csv_path)]) == 0
    out = capsys.readouterr().out
    assert "Win rate" in out
    assert csv_path.exists()
    snap = build_snapshot(cfg, include_exchange=False)
    assert "favorites" in snap
    assert snap["favorites"]["n_fills"] == 1
    assert snap["favorites"]["n_settled"] == 1
    assert "<h2>Favorites strategy</h2>" in PAGE
    assert "id=\"favorites\"" in PAGE


def test_v2_payload_no_favorite_is_ask_at_one_minus_p():
    from polymarket_bot.account_risk import worst_case_contract_risk
    from polymarket_bot.kalshi_orders import (
        create_order_v2_body,
        v2_from_paper_order,
        v2_would_cross,
    )

    no_order = PaperOrder(
        "o1",
        "KXRAIN-26SEP28-LV",
        "buy",
        Decimal("0.8500"),
        Decimal("2"),
        STRATEGY,
        contract_side="no",
        fill_tag=FILL_TAG,
    )
    book_side, yes_px = v2_from_paper_order(no_order)
    assert book_side == "ask"
    assert yes_px == Decimal("0.1500")
    assert worst_case_contract_risk(no_order.side, no_order.price, no_order.qty) == Decimal("1.70")
    body = create_order_v2_body(
        ticker="KXRAIN-26SEP28-LV",
        side="no",
        price="0.8500",
        count="2",
        post_only=True,
        client_order_id="pmbot-test-no",
    )
    assert body == {
        "ticker": "KXRAIN-26SEP28-LV",
        "side": "ask",
        "count": "2",
        "price": "0.1500",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": True,
        "client_order_id": "pmbot-test-no",
    }
    assert not v2_would_cross("ask", Decimal("0.1500"), Decimal("0.1400"), Decimal("0.1600"))
    assert v2_would_cross("ask", Decimal("0.1500"), Decimal("0.1500"), Decimal("0.1600"))

    yes_order = PaperOrder(
        "o2",
        "KXHIGHNY-29SEP26",
        "buy",
        Decimal("0.8500"),
        Decimal("1"),
        STRATEGY,
        contract_side="yes",
        fill_tag=FILL_TAG,
    )
    yes_side, yes_yes_px = v2_from_paper_order(yes_order)
    assert yes_side == "bid"
    assert yes_yes_px == Decimal("0.8500")
    yes_body = create_order_v2_body(
        ticker="KXHIGHNY-29SEP26",
        side="yes",
        price="0.8500",
        count="1",
        post_only=True,
        client_order_id="pmbot-test-yes",
    )
    assert yes_body["side"] == "bid"
    assert yes_body["price"] == "0.8500"
    assert yes_body["post_only"] is True
    two = create_order_v2_body(
        ticker="KXDEMO-COIN",
        side="bid",
        price="0.4900",
        count="1",
        post_only=True,
        client_order_id="pmbot-test-2s",
    )
    assert two["side"] == "bid"
    assert two["price"] == "0.4900"


def test_place_demo_order_converts_no_side_to_v2_ask():
    from polymarket_bot.exchanges.kalshi import KalshiClient

    cfg = load_config()
    client = KalshiClient(cfg)
    captured: dict = {}

    def signed(method, path, *, confirm_demo, body=None, params=None):
        captured["path"] = path
        captured["body"] = body
        return {"order": {"order_id": "oid-no"}}

    client.signed_demo = signed  # type: ignore[method-assign]
    try:
        client.place_demo_order(
            ticker="KXRAIN-26SEP28-LV",
            side="no",
            price="0.8500",
            count="2",
            confirm_demo=True,
            post_only=True,
            client_order_id="pmbot-no-fav",
        )
    finally:
        client.close()
    assert captured["path"] == "/portfolio/events/orders"
    assert captured["body"]["side"] == "ask"
    assert captured["body"]["price"] == "0.1500"
    assert captured["body"]["post_only"] is True
    assert captured["body"]["count"] == "2"


def test_favorites_scan_uses_series_list_not_first_800(caplog):
    from polymarket_bot.scanner import last_scan_summary, scan_maker_universe

    cfg = _fav_cfg()
    now = NOW
    weather = {
        "ticker": "KXRAIN-29SEP26-LV",
        "series_ticker": "KXRAIN",
        "title": "Las Vegas rain",
        "category": "Climate and Weather",
        "status": "open",
        "close_time": "2026-09-29T15:00:00Z",
        "fee_type": "quadratic",
        "volume_fp": "10",
    }
    junk = [
        {
            "ticker": f"KXJUNK-{i}",
            "series_ticker": "KXJUNK",
            "status": "open",
            "yes_bid_dollars": "0.49",
            "yes_ask_dollars": "0.51",
            "close_time": "2027-01-01T00:00:00Z",
            "fee_type": "quadratic",
            "volume_fp": "99999",
        }
        for i in range(20)
    ]

    class _Client:
        source_name = "fake"
        venue = "kalshi"

        def list_series(self, **_k):
            return [{"ticker": "KXRAIN"}, {"ticker": "KXNFLGAME"}]

        def list_markets(self, *, series_ticker=None, **_k):
            if series_ticker == "KXRAIN":
                return [weather]
            if series_ticker:
                return []
            return junk + [weather]

        def book(self, slug):
            return {"orderbook_fp": {"yes_dollars": [["0.1400", "12"]], "no_dollars": [["0.8400", "12"]]}}

        def snapshot(self, market, book=None, *, now=None):
            return snapshot_from_kalshi(market, book, now=now)

        def close(self):
            return None

    import logging

    caplog.set_level(logging.INFO, logger="polymarket_bot")
    rows = scan_maker_universe(_Client(), cfg, now=now)
    assert [s.slug for s in rows] == ["KXRAIN-29SEP26-LV"]
    summary = last_scan_summary()
    assert "favorites_scan" in summary
    assert "kept=1" in summary
    assert "by_reason=" in summary


def test_favorites_scan_logs_window_miss_without_loosening():
    from polymarket_bot.scanner import last_scan_summary, scan_maker_universe

    cfg = _fav_cfg()
    late = {
        "ticker": "KXRAIN-26SEP28-LV",
        "series_ticker": "KXRAIN",
        "title": "Las Vegas rain",
        "category": "Climate and Weather",
        "status": "open",
        "close_time": "2026-09-26T12:00:00Z",
        "fee_type": "quadratic",
    }

    class _Client:
        source_name = "fake"
        venue = "kalshi"

        def list_series(self, **_k):
            return [{"ticker": "KXRAIN"}]

        def list_markets(self, *, series_ticker=None, **_k):
            return [late] if series_ticker == "KXRAIN" else []

        def book(self, slug):
            raise AssertionError("outside-window market must not consume a book fetch")

        def snapshot(self, market, book=None, *, now=None):
            return snapshot_from_kalshi(market, book, now=now)

        def close(self):
            return None

    rows = scan_maker_universe(_Client(), cfg, now=NOW)
    assert rows == []
    summary = last_scan_summary()
    assert "favorites_window" in summary
    assert "kept=0" in summary
    assert "15m–6h" in summary or "15m-6h" in summary or "window" in summary
