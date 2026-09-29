"""Load the single YAML config file and overlay environment flags."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from polymarket_bot.account_risk import HARD_MAX_ACCOUNT_RISK_PCT, validate_account_risk_pct
from polymarket_bot.guard import assert_paper_only
from polymarket_bot.market_risk import (
    DEFAULT_MARKET_RISK_SCORE,
    HARD_MAX_MARKET_RISK_SCORE,
    validate_market_risk_score,
)


def _dec(value: Any, default: str | None = None) -> Decimal:
    if value is None:
        if default is None:
            raise ValueError("missing numeric config value")
        value = default
    return Decimal(str(value))


@dataclass(frozen=True)
class ApiConfig:
    gateway_base_url: str
    api_base_url: str
    request_timeout_seconds: float
    max_retries: int
    min_request_interval_seconds: float


@dataclass(frozen=True)
class ScannerConfig:
    active_only: bool
    include_closed: bool
    max_markets_to_list: int
    list_page_size: int
    max_list_pages: int
    max_book_fetches: int
    min_volume_shares: Decimal
    min_bid_depth_contracts: Decimal
    min_ask_depth_contracts: Decimal
    max_spread: Decimal
    min_hours_to_resolution: float
    top_n: int


QUOTE_SIZE_WARN = Decimal("5")
DEFAULT_REQUOTE_INTERVAL = 30.0
MIN_REQUOTE_INTERVAL = 10.0
MAX_REQUOTE_INTERVAL = 60.0
DEFAULT_HOURS_OVERRIDES = {
    "KXAAAGASD": 0.0,
    "KXAAAGASW": 0.0,
}


def clamp_requote_interval(seconds: float) -> float:
    return min(MAX_REQUOTE_INTERVAL, max(MIN_REQUOTE_INTERVAL, float(seconds)))


def merge_dict(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge_dict(out[key], value)
        else:
            out[key] = value
    return out


@dataclass(frozen=True)
class MakerConfig:
    enabled: bool
    half_spread: Decimal
    inventory_skew_per_contract: Decimal
    improve_ticks: int
    requote_interval_seconds: float
    requote_on_touch_ticks: int


@dataclass(frozen=True)
class NearResolutionConfig:
    enabled: bool
    min_price: Decimal
    max_price: Decimal
    max_hours_to_resolution: float
    min_hours_to_resolution: float
    quote_size_contracts: Decimal


@dataclass(frozen=True)
class RiskConfig:
    max_position_per_market: Decimal
    max_gross_position: Decimal
    max_daily_loss: Decimal
    max_daily_loss_usd: Decimal
    max_daily_capital_in_use_usd: Decimal
    cancel_on_price_jump: Decimal
    maker_min_hours_to_resolution: float
    max_account_risk_pct: Decimal
    allow_account_risk_above_hard_max: bool
    max_market_risk_score: Decimal
    allow_market_risk_above_hard_max: bool
    maker_min_hours_overrides: dict[str, float]
    kxhigh_resolution_day_enabled: bool


@dataclass(frozen=True)
class FillConfig:
    require_strict_trade_through: bool


@dataclass(frozen=True)
class PaperConfig:
    starting_cash: Decimal
    quote_size_contracts: Decimal
    max_markets: int
    ticks: int
    poll_interval_seconds: float
    tick_size_fallback: Decimal
    maker: MakerConfig
    near_resolution: NearResolutionConfig
    risk: RiskConfig
    fills: FillConfig


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    jsonl_path: Path
    report_path: Path
    state_path: Path
    demo_state_path: Path


@dataclass(frozen=True)
class KalshiConfig:
    market_data_base_url: str
    demo_base_url: str
    demo_orders_enabled: bool
    min_request_interval_seconds: float
    series_list_interval_seconds: float
    tick_size: Decimal


@dataclass(frozen=True)
class TradingConfig:
    enabled: bool
    toggle_path: Path
    lock_path: Path


@dataclass(frozen=True)
class DashboardConfig:
    host: str
    port: int
    refresh_seconds: float
    pnl_path: Path


@dataclass(frozen=True)
class RecordConfig:
    interval_seconds: float
    book_levels: int
    fmt: str
    directory: Path
    rotate_mb: float
    max_files: int
    data_host: str


@dataclass(frozen=True)
class CompareConfig:
    enabled: bool
    max_markets_each: int
    min_title_score: float
    min_net_edge: Decimal
    contract_size: Decimal


@dataclass(frozen=True)
class AppConfig:
    dry_run: bool
    live_trading_enabled: bool
    exchange: str
    polymarket_us_enabled: bool
    api: ApiConfig
    kalshi: KalshiConfig
    scanner: ScannerConfig
    paper: PaperConfig
    logging: LoggingConfig
    compare: CompareConfig
    trading: TradingConfig
    dashboard: DashboardConfig
    record: RecordConfig
    environment: str
    path: Path
    extra: dict[str, Any] = field(default_factory=dict)


def _repo_default_path(path: Path) -> bool:
    text = str(path)
    if text.startswith(("state/", "logs/", "data/")):
        return True
    try:
        resolved = path if path.is_absolute() else (Path.cwd() / path)
        resolved = resolved.resolve()
    except OSError:
        return False
    cwd = Path.cwd().resolve()
    for folder in ("state", "logs", "data"):
        try:
            resolved.relative_to(cwd / folder)
            return True
        except ValueError:
            continue
    return False


def _isolate_test_paths(config: AppConfig) -> None:
    """When pytest sets PMBOT_STATE_DIR, keep tests out of the repo state/logs dirs."""
    root = os.environ.get("PMBOT_STATE_DIR")
    if not root:
        return
    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)

    def rebase(path: Path, name: str) -> Path:
        return base / name if _repo_default_path(path) else path

    object.__setattr__(
        config,
        "trading",
        TradingConfig(
            config.trading.enabled,
            rebase(config.trading.toggle_path, "trading_toggle.json"),
            rebase(config.trading.lock_path, "trading.lock"),
        ),
    )
    object.__setattr__(
        config,
        "logging",
        LoggingConfig(
            config.logging.level,
            rebase(config.logging.jsonl_path, "decisions.jsonl"),
            rebase(config.logging.report_path, "paper_report.txt"),
            rebase(config.logging.state_path, "paper_state.json"),
            rebase(config.logging.demo_state_path, "demo_state.json"),
        ),
    )
    object.__setattr__(
        config,
        "dashboard",
        DashboardConfig(
            config.dashboard.host,
            config.dashboard.port,
            config.dashboard.refresh_seconds,
            rebase(config.dashboard.pnl_path, "pnl_history.jsonl"),
        ),
    )
    rec = config.record
    object.__setattr__(
        config,
        "record",
        RecordConfig(
            rec.interval_seconds,
            rec.book_levels,
            rec.fmt,
            rebase(rec.directory, "recordings"),
            rec.rotate_mb,
            rec.max_files,
            rec.data_host,
        ),
    )


def default_config_path() -> Path:
    cwd = Path.cwd() / "config.yaml"
    if cwd.exists():
        return cwd
    return Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path: str | Path | None = None, environment: str | None = None) -> AppConfig:
    load_dotenv()
    cfg_path = Path(path) if path else default_config_path()
    raw = yaml.safe_load(cfg_path.read_text()) or {}
    env_name = str(environment or os.environ.get("PMBOT_ENV") or raw.get("environment") or "paper")
    overlays = raw.get("environments") or {}
    if env_name in overlays and isinstance(overlays[env_name], dict):
        raw = merge_dict(raw, overlays[env_name])
    raw["environment"] = env_name

    api_raw = raw.get("api") or {}
    scan_raw = raw.get("scanner") or {}
    paper_raw = raw.get("paper") or {}
    maker_raw = paper_raw.get("maker") or {}
    near_raw = paper_raw.get("near_resolution") or {}
    risk_raw = paper_raw.get("risk") or {}
    fills_raw = paper_raw.get("fills") or {}
    log_raw = raw.get("logging") or {}
    kalshi_raw = raw.get("kalshi") or {}
    compare_raw = raw.get("compare") or {}
    trading_raw = raw.get("trading") or {}
    dash_raw = raw.get("dashboard") or {}
    record_raw = raw.get("record") or {}

    risk_pct = _dec(risk_raw.get("max_account_risk_pct"), str(HARD_MAX_ACCOUNT_RISK_PCT))
    allow_above = bool(risk_raw.get("allow_account_risk_above_hard_max", False))
    validate_account_risk_pct(risk_pct, allow_above_hard_max=allow_above)
    market_score = _dec(risk_raw.get("max_market_risk_score"), str(DEFAULT_MARKET_RISK_SCORE))
    allow_market_above = bool(risk_raw.get("allow_market_risk_above_hard_max", False))
    validate_market_risk_score(market_score, allow_above_hard_max=allow_market_above)

    hours_overrides = dict(DEFAULT_HOURS_OVERRIDES)
    raw_overrides = risk_raw.get("maker_min_hours_overrides") or {}
    if isinstance(raw_overrides, dict):
        hours_overrides.update({str(k).upper(): float(v) for k, v in raw_overrides.items()})

    quote_size = _dec(paper_raw.get("quote_size_contracts"), "1")
    if quote_size > QUOTE_SIZE_WARN:
        logging.getLogger("polymarket_bot").warning(
            "quote_size_contracts=%s is above %s; backtests used size 1",
            quote_size,
            QUOTE_SIZE_WARN,
        )

    config = AppConfig(
        dry_run=bool(raw.get("dry_run", True)),
        live_trading_enabled=bool(raw.get("live_trading_enabled", False)),
        exchange=str(raw.get("exchange", "kalshi")),
        polymarket_us_enabled=bool(raw.get("polymarket_us_enabled", False)),
        api=ApiConfig(
            gateway_base_url=str(api_raw.get("gateway_base_url", "https://gateway.polymarket.us")),
            api_base_url=str(api_raw.get("api_base_url", "https://api.polymarket.us")),
            request_timeout_seconds=float(api_raw.get("request_timeout_seconds", 30)),
            max_retries=int(api_raw.get("max_retries", 4)),
            min_request_interval_seconds=float(api_raw.get("min_request_interval_seconds", 0.35)),
        ),
        kalshi=KalshiConfig(
            market_data_base_url=str(
                kalshi_raw.get("market_data_base_url", "https://external-api.kalshi.com/trade-api/v2")
            ),
            demo_base_url=str(
                kalshi_raw.get("demo_base_url", "https://demo-api.kalshi.co/trade-api/v2")
            ),
            demo_orders_enabled=bool(kalshi_raw.get("demo_orders_enabled", False)),
            min_request_interval_seconds=float(kalshi_raw.get("min_request_interval_seconds", 0.08)),
            series_list_interval_seconds=float(kalshi_raw.get("series_list_interval_seconds", 0.35)),
            tick_size=_dec(kalshi_raw.get("tick_size"), "0.01"),
        ),
        scanner=ScannerConfig(
            active_only=bool(scan_raw.get("active_only", True)),
            include_closed=bool(scan_raw.get("include_closed", False)),
            max_markets_to_list=int(scan_raw.get("max_markets_to_list", 800)),
            list_page_size=int(scan_raw.get("list_page_size", 200)),
            max_list_pages=int(scan_raw.get("max_list_pages", 6)),
            max_book_fetches=int(scan_raw.get("max_book_fetches", 10)),
            min_volume_shares=_dec(scan_raw.get("min_volume_shares"), "1000"),
            min_bid_depth_contracts=_dec(scan_raw.get("min_bid_depth_contracts"), "25"),
            min_ask_depth_contracts=_dec(scan_raw.get("min_ask_depth_contracts"), "25"),
            max_spread=_dec(scan_raw.get("max_spread"), "0.04"),
            min_hours_to_resolution=float(scan_raw.get("min_hours_to_resolution", 2)),
            top_n=int(scan_raw.get("top_n", 12)),
        ),
        paper=PaperConfig(
            starting_cash=_dec(paper_raw.get("starting_cash"), "1000"),
            quote_size_contracts=quote_size,
            max_markets=int(paper_raw.get("max_markets", 5)),
            ticks=int(paper_raw.get("ticks", 6)),
            poll_interval_seconds=float(paper_raw.get("poll_interval_seconds", 2.0)),
            tick_size_fallback=_dec(paper_raw.get("tick_size_fallback"), "0.001"),
            maker=MakerConfig(
                enabled=bool(maker_raw.get("enabled", True)),
                half_spread=_dec(maker_raw.get("half_spread"), "0.02"),
                inventory_skew_per_contract=_dec(maker_raw.get("inventory_skew_per_contract"), "0.0004"),
                improve_ticks=int(maker_raw.get("improve_ticks", 1)),
                requote_interval_seconds=clamp_requote_interval(
                    float(maker_raw.get("requote_interval_seconds", DEFAULT_REQUOTE_INTERVAL))
                ),
                requote_on_touch_ticks=int(maker_raw.get("requote_on_touch_ticks", 1)),
            ),
            near_resolution=NearResolutionConfig(
                enabled=bool(near_raw.get("enabled", True)),
                min_price=_dec(near_raw.get("min_price"), "0.94"),
                max_price=_dec(near_raw.get("max_price"), "0.98"),
                max_hours_to_resolution=float(near_raw.get("max_hours_to_resolution", 36)),
                min_hours_to_resolution=float(near_raw.get("min_hours_to_resolution", 0.5)),
                quote_size_contracts=_dec(near_raw.get("quote_size_contracts"), "5"),
            ),
            risk=RiskConfig(
                max_position_per_market=_dec(risk_raw.get("max_position_per_market"), "40"),
                max_gross_position=_dec(risk_raw.get("max_gross_position"), "120"),
                max_daily_loss=_dec(
                    risk_raw.get("max_daily_loss_usd", risk_raw.get("max_daily_loss")),
                    "50",
                ),
                max_daily_loss_usd=_dec(
                    risk_raw.get("max_daily_loss_usd", risk_raw.get("max_daily_loss")),
                    "50",
                ),
                max_daily_capital_in_use_usd=_dec(
                    risk_raw.get("max_daily_capital_in_use_usd"), "100"
                ),
                cancel_on_price_jump=_dec(risk_raw.get("cancel_on_price_jump"), "0.08"),
                maker_min_hours_to_resolution=float(risk_raw.get("maker_min_hours_to_resolution", 24)),
                max_account_risk_pct=risk_pct,
                allow_account_risk_above_hard_max=allow_above,
                max_market_risk_score=market_score,
                allow_market_risk_above_hard_max=allow_market_above,
                maker_min_hours_overrides=hours_overrides,
                kxhigh_resolution_day_enabled=bool(
                    (paper_raw.get("series") or {}).get("kxhigh_resolution_day_enabled")
                    or risk_raw.get("kxhigh_resolution_day_enabled", False)
                ),
            ),
            fills=FillConfig(
                require_strict_trade_through=bool(fills_raw.get("require_strict_trade_through", True)),
            ),
        ),
        logging=LoggingConfig(
            level=str(log_raw.get("level", "INFO")),
            jsonl_path=Path(log_raw.get("jsonl_path", "logs/decisions.jsonl")),
            report_path=Path(log_raw.get("report_path", "logs/paper_report.txt")),
            state_path=Path(log_raw.get("state_path", "logs/paper_state.json")),
            demo_state_path=Path(log_raw.get("demo_state_path", "logs/demo_state.json")),
        ),
        compare=CompareConfig(
            enabled=bool(compare_raw.get("enabled", False)),
            max_markets_each=int(compare_raw.get("max_markets_each", 40)),
            min_title_score=float(compare_raw.get("min_title_score", 0.34)),
            min_net_edge=_dec(compare_raw.get("min_net_edge"), "0.01"),
            contract_size=_dec(compare_raw.get("contract_size"), "100"),
        ),
        trading=TradingConfig(
            enabled=bool(trading_raw.get("enabled", True)),
            toggle_path=Path(trading_raw.get("toggle_path", "state/trading_toggle.json")),
            lock_path=Path(trading_raw.get("lock_path", "state/trading.lock")),
        ),
        dashboard=DashboardConfig(
            host=str(dash_raw.get("host", "127.0.0.1")),
            port=int(dash_raw.get("port", 8787)),
            refresh_seconds=float(dash_raw.get("refresh_seconds", 3)),
            pnl_path=Path(dash_raw.get("pnl_path", "logs/pnl_history.jsonl")),
        ),
        record=RecordConfig(
            interval_seconds=float(record_raw.get("interval_seconds", 5)),
            book_levels=int(record_raw.get("book_levels", 5)),
            fmt=str(record_raw.get("format", "jsonl")).lower(),
            directory=Path(record_raw.get("directory", "data/recordings")),
            rotate_mb=float(record_raw.get("rotate_mb", 64)),
            max_files=int(record_raw.get("max_files", 20)),
            data_host=str(record_raw.get("data_host", "production")).lower(),
        ),
        environment=env_name,
        path=cfg_path,
        extra=raw,
    )
    _isolate_test_paths(config)
    assert_paper_only(
        dry_run=config.dry_run,
        live_trading_enabled=config.live_trading_enabled,
        env_flags=[
            os.environ.get("POLYMARKET_LIVE_TRADING"),
            os.environ.get("KALSHI_LIVE_TRADING"),
        ],
    )
    return config
