"""Maker-universe series allow/deny, event blackouts, and fee-flag skip.

The allowlist feeds the maker gate. It does not replace the per-market risk score.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from polymarket_bot.config import AppConfig
from polymarket_bot.market_data import MarketSnapshot

PT = ZoneInfo("America/Los_Angeles")

DEFAULT_ALLOW = (
    "KXHIGH",
    "KXBRENTW",
    "KXRT",
    "KXU3",
    "KXPAYROLLS",
    "KXDEMO",
)
DEFAULT_DISABLED = (
    "KXTRUTHSOCIAL",
    "KXBTC",
    "KXETHD",
    "KXAAAGASD",
    "KXAAAGASW",
)
DEFAULT_DENY = (
    "KXNFL",
    "KXNBA",
    "KXMLB",
    "KXNHL",
    "KXNCAA",
    "KXWNBA",
    "KXGAME",
    "KXFEDDECISION",
    "KXFED",
    "KXCPI",
    "CPI",
    "KXWTI",
    "KXINXU",
    "KXBTC15M",
    "KXBTCD",
    "KXXRPD",
    "KXRAIN",
    "KXEPL",
    "KXSOCCER",
)
_SPORTS_DENY = ("GAME", "NFL", "NBA", "MLB", "NHL", "NCAA", "WNBA", "EPL")
_MENTION = ("mention", "will say", "will x say", "say that")
_ENTERTAIN = ("ranking", "box office", "oscar", "grammy", "emmy", "rotten tomatoes")
_MAKER_FEE = ("quadratic_with_maker", "maker_fee")


def series_ticker(snap: MarketSnapshot) -> str:
    market = (snap.raw or {}).get("market") or {}
    return str(market.get("series_ticker") or snap.slug or "").upper()


def _prefixes(config: AppConfig) -> tuple[list[str], list[str]]:
    extra = (config.extra.get("paper") or {}).get("series") or {}
    allow = [str(x).upper() for x in (extra.get("allow") or DEFAULT_ALLOW)]
    deny = [str(x).upper() for x in (extra.get("deny") or DEFAULT_DENY)]
    return allow, deny


def _starts_with_any(text: str, prefixes: list[str] | tuple[str, ...]) -> bool:
    return any(text.startswith(p) or p in text for p in prefixes if p)


def filter_enabled(config: AppConfig) -> bool:
    extra = (config.extra.get("paper") or {}).get("series") or {}
    if "enabled" in extra:
        return bool(extra.get("enabled"))
    return True


def applies_to(snap: MarketSnapshot) -> bool:
    slug = (snap.slug or "").upper()
    if snap.venue == "polymarket_us":
        return False
    if slug.startswith("DEMO-") or slug in {"M", "FAV"}:
        return False
    series = series_ticker(snap)
    return series.startswith("KX") or slug.startswith("KX")


def _is_denied(snap: MarketSnapshot, deny: list[str]) -> bool:
    series = series_ticker(snap)
    slug = (snap.slug or "").upper()
    blob = " ".join(
        filter(None, [snap.slug, snap.question, snap.event_title, snap.category, series])
    ).lower()
    for prefix in deny:
        if series.startswith(prefix) or slug.startswith(prefix):
            return True
    if any(tok.lower() in series or tok.lower() in slug for tok in _SPORTS_DENY):
        return True
    if any(tok in blob for tok in _MENTION):
        return True
    if any(tok in blob for tok in _ENTERTAIN):
        return True
    return False


def _is_allowed(snap: MarketSnapshot, allow: list[str]) -> bool:
    series = series_ticker(snap)
    slug = (snap.slug or "").upper()
    for prefix in allow:
        if series.startswith(prefix) or slug.startswith(prefix):
            if prefix == "KXBTC" and (series.startswith("KXBTC15M") or series.startswith("KXBTCD") or slug.startswith("KXBTC15M") or slug.startswith("KXBTCD")):
                continue
            return True
    return False


def kxhigh_resolution_day_enabled(config: AppConfig) -> bool:
    extra = (config.extra.get("paper") or {}).get("series") or {}
    if "kxhigh_resolution_day_enabled" in extra:
        return bool(extra.get("kxhigh_resolution_day_enabled"))
    return bool(config.paper.risk.kxhigh_resolution_day_enabled)


def is_kxhigh_same_day(snap: MarketSnapshot) -> bool:
    series = series_ticker(snap)
    slug = (snap.slug or "").upper()
    if not (series.startswith("KXHIGH") or slug.startswith("KXHIGH")):
        return False
    hours = snap.hours_to_resolution
    return hours is not None and hours < 24.0


def _kxhigh_ok(snap: MarketSnapshot, config: AppConfig) -> bool:
    hours = snap.hours_to_resolution
    if hours is None:
        return False
    if hours > 40.0:
        return False
    if hours >= 24.0:
        return True
    return kxhigh_resolution_day_enabled(config) and hours >= 4.0


def maker_min_hours(snap: MarketSnapshot, config: AppConfig) -> float:
    """Per-series hours floor. Default 24h; gas uses AAA blackout; KXHIGH same-day is 4h only if opted in."""
    series = series_ticker(snap)
    slug = (snap.slug or "").upper()
    if (series.startswith("KXHIGH") or slug.startswith("KXHIGH")) and kxhigh_resolution_day_enabled(config):
        return 4.0
    overrides = dict(config.paper.risk.maker_min_hours_overrides or {})
    for prefix, hours in sorted(overrides.items(), key=lambda kv: -len(str(kv[0]))):
        token = str(prefix).upper()
        if series.startswith(token) or slug.startswith(token):
            return float(hours)
    return float(config.paper.risk.maker_min_hours_to_resolution)


def _kxrt_ok(snap: MarketSnapshot) -> bool:
    mid = snap.mid
    if mid is None:
        return False
    return Decimal("0.02") < mid < Decimal("0.98")


def _has_maker_fees(snap: MarketSnapshot) -> bool:
    fee = (snap.fee_type or "").lower()
    return any(tok in fee for tok in _MAKER_FEE)


def _in_blackout(now: datetime, at: datetime, minutes: int) -> bool:
    delta = abs((now - at).total_seconds())
    return delta <= minutes * 60


def event_blackout(snap: MarketSnapshot, config: AppConfig, now: datetime) -> bool:
    extra = (config.extra.get("paper") or {}).get("series") or {}
    minutes = int(extra.get("event_blackout_minutes", 30))
    events = extra.get("events") or [
        {"name": "jobs", "at": "2026-10-02T12:30:00+00:00", "series": ["KXHIGH", "KXRT", "KXBRENTW", "KXU3", "KXPAYROLLS"]},
        {"name": "cpi", "at": "2026-10-14T12:30:00+00:00", "series": ["KXRT", "KXBTC", "KXETHD"]},
        {"name": "fomc", "at": "2026-10-28T18:00:00+00:00", "series": ["KXFED", "KXFEDDECISION"]},
    ]
    series = series_ticker(snap)
    for event in events:
        raw_at = event.get("at")
        if not raw_at:
            continue
        try:
            when = datetime.fromisoformat(str(raw_at).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if not _in_blackout(now, when, minutes):
            continue
        prefixes = [str(x).upper() for x in (event.get("series") or [])]
        if not prefixes or _starts_with_any(series, prefixes):
            return True
    if gas_blackout(snap, config, now):
        return True
    return False


def _hhmm_minutes(value: Any, default: str) -> int:
    raw = str(value or default)
    hh, mm = raw.split(":")
    return int(hh) * 60 + int(mm)


def _in_hhmm_window(now_minutes: int, start: int, end: int) -> bool:
    if start <= end:
        return start <= now_minutes <= end
    return now_minutes >= start or now_minutes <= end


def gas_blackout(snap: MarketSnapshot, config: AppConfig, now: datetime) -> bool:
    """AAA gasoline: evening pre-close on daily, morning window for anything still open."""
    series = series_ticker(snap)
    slug = (snap.slug or "").upper()
    if not (series.startswith("KXAAA") or slug.startswith("KXAAA")):
        return False
    extra = (config.extra.get("paper") or {}).get("series") or {}
    local = now.astimezone(PT)
    mins = local.hour * 60 + local.minute
    eve_start = _hhmm_minutes(extra.get("aaa_evening_stop"), "20:00")
    eve_end = _hhmm_minutes(extra.get("aaa_close"), "20:59")
    morn_start = _hhmm_minutes(extra.get("aaa_morning_blackout_start"), "03:30")
    morn_end = _hhmm_minutes(extra.get("aaa_morning_blackout_end"), "07:00")
    if series.startswith("KXAAAGASD") or slug.startswith("KXAAAGASD"):
        if _in_hhmm_window(mins, eve_start, eve_end):
            return True
    return _in_hhmm_window(mins, morn_start, morn_end)


def touch_queue(snap: MarketSnapshot) -> Decimal:
    bid = snap.bids[0].qty if snap.bids else Decimal("0")
    ask = snap.asks[0].qty if snap.asks else Decimal("0")
    if bid and ask:
        return min(bid, ask)
    return bid or ask or Decimal("0")


def listed_market_ok(
    market: dict[str, Any],
    config: AppConfig,
    *,
    now: datetime | None = None,
) -> bool:
    """Pre-book allow/deny using list metadata so tennis/NFL never consume book fetches."""
    from polymarket_bot.exchanges.kalshi import snapshot_from_kalshi

    snap = snapshot_from_kalshi(market, None, now=now)
    if not filter_enabled(config) or not applies_to(snap):
        return True
    allow, deny = _prefixes(config)
    extra = (config.extra.get("paper") or {}).get("series") or {}
    skip_fees = bool(extra.get("skip_maker_fee_series", True))
    if skip_fees and _has_maker_fees(snap):
        return False
    if _is_denied(snap, deny):
        return False
    if not _is_allowed(snap, allow):
        return False
    return True


def maker_universe_ok(
    snap: MarketSnapshot,
    config: AppConfig,
    *,
    now: datetime | None = None,
) -> bool:
    """True if the maker may consider this market. Risk score is still required."""
    if not filter_enabled(config) or not applies_to(snap):
        return True
    now = now or datetime.now(timezone.utc)
    allow, deny = _prefixes(config)
    extra = (config.extra.get("paper") or {}).get("series") or {}
    skip_fees = bool(extra.get("skip_maker_fee_series", True))
    if skip_fees and _has_maker_fees(snap):
        return False
    if _is_denied(snap, deny):
        return False
    if not _is_allowed(snap, allow):
        return False
    series = series_ticker(snap)
    if series.startswith("KXHIGH") and not _kxhigh_ok(snap, config):
        return False
    if series.startswith("KXRT") and not _kxrt_ok(snap):
        return False
    if event_blackout(snap, config, now):
        return False
    return True


def skip_reason(
    snap: MarketSnapshot,
    config: AppConfig,
    *,
    now: datetime | None = None,
) -> str | None:
    if maker_universe_ok(snap, config, now=now):
        return None
    return "series_filter"
