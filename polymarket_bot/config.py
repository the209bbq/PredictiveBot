"""Load the single YAML config file and overlay environment flags."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from polymarket_bot.guard import assert_paper_only


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
class AppConfig:
    dry_run: bool
    live_trading_enabled: bool
    api: ApiConfig
    scanner: ScannerConfig
    paper: PaperConfig
    logging: LoggingConfig
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

    config = AppConfig(
        dry_run=bool(raw.get("dry_run", True)),
        live_trading_enabled=bool(raw.get("live_trading_enabled", False)),
        api=ApiConfig(
            gateway_base_url=str(api_raw.get("gateway_base_url", "https://gateway.polymarket.us")),
            api_base_url=str(api_raw.get("api_base_url", "https://api.polymarket.us")),
            request_timeout_seconds=float(api_raw.get("request_timeout_seconds", 30)),
            max_retries=int(api_raw.get("max_retries", 2)),
            min_request_interval_seconds=float(api_raw.get("min_request_interval_seconds", 0.08)),
        ),
        scanner=ScannerConfig(
            active_only=bool(scan_raw.get("active_only", True)),
            include_closed=bool(scan_raw.get("include_closed", False)),
            max_markets_to_list=int(scan_raw.get("max_markets_to_list", 40)),
            max_book_fetches=int(scan_raw.get("max_book_fetches", 20)),
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
        path=cfg_path,
        extra=raw,
    )
    assert_paper_only(
        dry_run=config.dry_run,
        live_trading_enabled=config.live_trading_enabled,
        env_live=os.environ.get("POLYMARKET_LIVE_TRADING"),
    )
    return config
