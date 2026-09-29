"""Maker-universe series allow/deny, event blackouts, and fee-flag skip.

The allowlist feeds the maker gate. It does not replace the per-market risk score.

The bot is price-based. It does not ingest social, political, or review-site
feeds. If external data is added later, it must come from official statistical
APIs only (NWS / EIA / BLS).
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
_DEFAULT_BRENT = {
    "weekend_halt_start": "14:00",
    "weekend_halt_end": "15:00",
    "unwind_start": "12:00",
    "friday_settle": "14:00",
    "recurring_blackouts": [
        {"name": "api_inventory", "weekday": "tue", "start": "13:00", "end": "14:30"},
        {"name": "eia_weekly", "weekday": "wed", "start": "07:00", "end": "08:30"},
    ],
    "dated_blackouts": [
        {
            "name": "eia_weekly",
            "at": "2026-10-15T09:00:00",
            "minutes": 90,
            "replaces": "eia_weekly",
        },
        {"name": "eia_steo", "at": "2026-10-06T09:00:00", "minutes": 90},
        {"name": "opec_jmmc", "date": "2026-10-04", "all_day": True},
    ],
}
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def series_ticker(snap: MarketSnapshot) -> str:
    market = (snap.raw or {}).get("market") or {}
    return str(market.get("series_ticker") or snap.slug or "").upper()


def _prefixes(config: AppConfig) -> tuple[list[str], list[str]]:
    extra = (config.extra.get("paper") or {}).get("series") or {}
    allow = [str(x).upper() for x in (extra.get("allow") or DEFAULT_ALLOW)]
    deny = [str(x).upper() for x in (extra.get("deny") or DEFAULT_DENY)]
    return allow, deny


def _disabled_list(config: AppConfig) -> list[str]:
    extra = (config.extra.get("paper") or {}).get("series") or {}
    if "disabled" in extra:
        return [str(x).upper() for x in (extra.get("disabled") or [])]
    return [str(x).upper() for x in DEFAULT_DISABLED]


def _is_disabled(snap: MarketSnapshot, disabled: list[str]) -> bool:
    series = series_ticker(snap)
    slug = (snap.slug or "").upper()
    return any(series.startswith(p) or slug.startswith(p) for p in disabled if p)


def reducing_quote(side: str, qty) -> bool:
    """True if this maker side reduces an existing position (unwind-only)."""
    if qty > 0:
        return side == "sell"
    if qty < 0:
        return side == "buy"
    return False


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


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now


def _local_pt(now: datetime) -> datetime:
    return _aware(now).astimezone(PT)


def _in_blackout(now: datetime, at: datetime, minutes: int) -> bool:
    delta = abs((_aware(now) - _aware(at)).total_seconds())
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
    now = _aware(now)
    for event in events:
        raw_at = event.get("at")
        if not raw_at:
            continue
        when = _parse_pt_datetime(raw_at)
        if when is None:
            continue
        if not _in_blackout(now, when, minutes):
            continue
        prefixes = [str(x).upper() for x in (event.get("series") or [])]
        if not prefixes or _starts_with_any(series, prefixes):
            return True
    if gas_blackout(snap, config, now):
        return True
    if brent_halt(snap, config, now):
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


def _parse_pt_datetime(raw: Any) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=PT)
    return when


def _weekday_index(value: Any) -> int | None:
    token = str(value or "").strip().lower()[:3]
    if token in _WEEKDAYS:
        return _WEEKDAYS.index(token)
    return None


def is_brent(snap: MarketSnapshot) -> bool:
    series = series_ticker(snap)
    slug = (snap.slug or "").upper()
    return series.startswith("KXBRENTW") or slug.startswith("KXBRENTW")


def _brent_settings(config: AppConfig) -> dict[str, Any]:
    extra = (config.extra.get("paper") or {}).get("series") or {}
    raw = extra.get("brent") if isinstance(extra.get("brent"), dict) else {}
    out = dict(_DEFAULT_BRENT)
    out.update(raw)
    if not raw.get("recurring_blackouts"):
        out["recurring_blackouts"] = list(_DEFAULT_BRENT["recurring_blackouts"])
    if not raw.get("dated_blackouts"):
        out["dated_blackouts"] = list(_DEFAULT_BRENT["dated_blackouts"])
    return out


def _brent_weekend_halt(local: datetime, settings: dict[str, Any]) -> bool:
    start = _hhmm_minutes(settings.get("weekend_halt_start"), "14:00")
    end = _hhmm_minutes(settings.get("weekend_halt_end"), "15:00")
    mins = local.hour * 60 + local.minute
    wd = local.weekday()
    if wd == 4:
        return mins >= start
    if wd == 5:
        return True
    if wd == 6:
        return mins <= end
    return False


def _brent_unwind_window(local: datetime, settings: dict[str, Any]) -> bool:
    if local.weekday() != 4:
        return False
    start = _hhmm_minutes(settings.get("unwind_start"), "12:00")
    settle = _hhmm_minutes(settings.get("friday_settle"), "14:00")
    mins = local.hour * 60 + local.minute
    return start <= mins < settle


def _iso_week(day) -> tuple[int, int]:
    cal = day.isocalendar()
    return (int(cal[0]), int(cal[1]))


def _dated_replaces_this_week(local: datetime, settings: dict[str, Any]) -> set[str]:
    skipped: set[str] = set()
    local_week = _iso_week(local.date())
    for event in settings.get("dated_blackouts") or []:
        replaces = str(event.get("replaces") or "").strip().lower()
        if not replaces:
            continue
        when = None
        if event.get("at"):
            when = _parse_pt_datetime(event.get("at"))
        elif event.get("date"):
            try:
                when = datetime.fromisoformat(str(event.get("date"))).replace(tzinfo=PT)
            except ValueError:
                when = None
        if when is None:
            continue
        if _iso_week(when.astimezone(PT).date()) == local_week:
            skipped.add(replaces)
    return skipped


def _brent_recurring_halt(local: datetime, settings: dict[str, Any]) -> bool:
    mins = local.hour * 60 + local.minute
    skipped = _dated_replaces_this_week(local, settings)
    for event in settings.get("recurring_blackouts") or []:
        name = str(event.get("name") or "").strip().lower()
        if name and name in skipped:
            continue
        wd = _weekday_index(event.get("weekday"))
        if wd is None or local.weekday() != wd:
            continue
        start = _hhmm_minutes(event.get("start"), "00:00")
        end = _hhmm_minutes(event.get("end"), "00:00")
        if _in_hhmm_window(mins, start, end):
            return True
    return False


def _brent_dated_halt(local: datetime, settings: dict[str, Any]) -> bool:
    local_date = local.date()
    for event in settings.get("dated_blackouts") or []:
        if event.get("all_day") or (event.get("date") and not event.get("at")):
            raw = event.get("date") or (str(event.get("at") or "")[:10])
            try:
                day = datetime.fromisoformat(str(raw)[:10]).date()
            except ValueError:
                continue
            if day == local_date:
                return True
            continue
        when = _parse_pt_datetime(event.get("at"))
        if when is None:
            continue
        minutes = int(event.get("minutes") or 90)
        start = when.astimezone(PT)
        end = start + timedelta(minutes=minutes)
        if start <= local <= end:
            return True
    return False


def brent_halt(snap: MarketSnapshot, config: AppConfig, now: datetime) -> bool:
    """True when KXBRENTW must not open or keep resting quotes."""
    if not is_brent(snap):
        return False
    settings = _brent_settings(config)
    local = _local_pt(now)
    if _brent_weekend_halt(local, settings):
        return True
    if _brent_recurring_halt(local, settings):
        return True
    if _brent_dated_halt(local, settings):
        return True
    return False


def brent_unwind_only(snap: MarketSnapshot, config: AppConfig, now: datetime) -> bool:
    """Friday final two hours before 2:00 PM PT settlement — reduce only."""
    if not is_brent(snap) or brent_halt(snap, config, now):
        return False
    return _brent_unwind_window(_local_pt(now), _brent_settings(config))


def quote_mode(snap: MarketSnapshot, config: AppConfig, now: datetime) -> str:
    """halt | unwind | ok for the current maker quote."""
    if event_blackout(snap, config, now):
        return "halt"
    if brent_unwind_only(snap, config, now):
        return "unwind"
    return "ok"


def gas_blackout(snap: MarketSnapshot, config: AppConfig, now: datetime) -> bool:
    """AAA gasoline. Risk is late-evening pre-close, not the morning print.

    Daily (KXAAAGASD) trading closes 8:59 PM PT the night before. Halt daily
    quoting 8:00–8:59 PM PT. AAA prints ~4:06 AM PT (outliers to ~6:10 AM);
    weekly KXAAAGASW settles Monday ~5:39–6:11 AM PT. Halt anything still
    open 3:30–7:00 AM PT. Series stay off the allowlist unless re-enabled.
    """
    series = series_ticker(snap)
    slug = (snap.slug or "").upper()
    if not (series.startswith("KXAAA") or slug.startswith("KXAAA")):
        return False
    extra = (config.extra.get("paper") or {}).get("series") or {}
    local = _local_pt(now)
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
    if _is_disabled(snap, _disabled_list(config)):
        return False
    if not _is_allowed(snap, allow):
        return False
    if event_blackout(snap, config, now or datetime.now(timezone.utc)):
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
    if _is_disabled(snap, _disabled_list(config)):
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
