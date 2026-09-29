"""Load the single YAML config file and overlay environment flags."""

from __future__ import annotations

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


@dataclass(frozen=True)
class MakerConfig:
    enabled: bool
    half_spread: Decimal
    inventory_skew_per_contract: Decimal
    improve_ticks: int


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
    cancel_on_price_jump: Decimal
    maker_min_hours_to_resolution: float
    max_account_risk_pct: Decimal
    allow_account_risk_above_hard_max: bool
    max_market_risk_score: Decimal
    allow_market_risk_above_hard_max: bool


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


@dataclass(frozen=True)
class KalshiConfig:
    market_data_base_url: str
    demo_base_url: str
    demo_orders_enabled: bool
    min_request_interval_seconds: float
    tick_size: Decimal


@dataclass(frozen=True)
class TradingConfig:
    enabled: bool
    toggle_path: Path
    lock_path: Path


@dataclass(frozen=True)
class CompareConfig:
    max_markets_each: int
    min_title_score: float
    min_net_edge: Decimal
    contract_size: Decimal


@dataclass(frozen=True)
class AppConfig:
    dry_run: bool
    live_trading_enabled: bool
    exchange: str
    api: ApiConfig
    kalshi: KalshiConfig
    scanner: ScannerConfig
    paper: PaperConfig
    logging: LoggingConfig
    compare: CompareConfig
    trading: TradingConfig
    path: Path
    extra: dict[str, Any] = field(default_factory=dict)


def default_config_path() -> Path:
    cwd = Path.cwd() / "config.yaml"
    if cwd.exists():
        return cwd
    return Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path: str | Path | None = None) -> AppConfig:
    load_dotenv()
    cfg_path = Path(path) if path else default_config_path()
    raw = yaml.safe_load(cfg_path.read_text()) or {}

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

    risk_pct = _dec(risk_raw.get("max_account_risk_pct"), str(HARD_MAX_ACCOUNT_RISK_PCT))
    allow_above = bool(risk_raw.get("allow_account_risk_above_hard_max", False))
    validate_account_risk_pct(risk_pct, allow_above_hard_max=allow_above)
    market_score = _dec(risk_raw.get("max_market_risk_score"), str(DEFAULT_MARKET_RISK_SCORE))
    allow_market_above = bool(risk_raw.get("allow_market_risk_above_hard_max", False))
    validate_market_risk_score(market_score, allow_above_hard_max=allow_market_above)

    config = AppConfig(
        dry_run=bool(raw.get("dry_run", True)),
        live_trading_enabled=bool(raw.get("live_trading_enabled", False)),
        exchange=str(raw.get("exchange", "kalshi")),
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
            quote_size_contracts=_dec(paper_raw.get("quote_size_contracts"), "10"),
            max_markets=int(paper_raw.get("max_markets", 5)),
            ticks=int(paper_raw.get("ticks", 6)),
            poll_interval_seconds=float(paper_raw.get("poll_interval_seconds", 2.0)),
            tick_size_fallback=_dec(paper_raw.get("tick_size_fallback"), "0.001"),
            maker=MakerConfig(
                enabled=bool(maker_raw.get("enabled", True)),
                half_spread=_dec(maker_raw.get("half_spread"), "0.02"),
                inventory_skew_per_contract=_dec(maker_raw.get("inventory_skew_per_contract"), "0.0004"),
                improve_ticks=int(maker_raw.get("improve_ticks", 1)),
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
                max_daily_loss=_dec(risk_raw.get("max_daily_loss"), "25"),
                cancel_on_price_jump=_dec(risk_raw.get("cancel_on_price_jump"), "0.08"),
                maker_min_hours_to_resolution=float(risk_raw.get("maker_min_hours_to_resolution", 6)),
                max_account_risk_pct=risk_pct,
                allow_account_risk_above_hard_max=allow_above,
                max_market_risk_score=market_score,
                allow_market_risk_above_hard_max=allow_market_above,
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
        ),
        compare=CompareConfig(
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
        path=cfg_path,
        extra=raw,
    )
    assert_paper_only(
        dry_run=config.dry_run,
        live_trading_enabled=config.live_trading_enabled,
        env_flags=[
            os.environ.get("POLYMARKET_LIVE_TRADING"),
            os.environ.get("KALSHI_LIVE_TRADING"),
        ],
    )
    return config
