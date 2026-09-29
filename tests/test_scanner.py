from datetime import datetime, timezone
from decimal import Decimal

from polymarket_bot.config import load_config
from polymarket_bot.exchanges.kalshi import snapshot_from_kalshi
from polymarket_bot.market_data.errors import BookFetchError
from polymarket_bot.market_data.fixture_client import FixtureClient
from polymarket_bot.scanner import format_scan_table, is_liquid, liquidity_score, scan_markets, scan_near_resolution


def test_scan_fixtures_returns_liquid_rows():
    cfg = load_config()
    client = FixtureClient("fixtures/public_markets.json")
    now = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)
    rows = scan_markets(client, cfg, now=now)
    assert rows
    assert all(is_liquid(s, cfg.scanner) for s in rows)
    assert all(s.spread is not None and s.spread <= cfg.scanner.max_spread for s in rows)
    assert all(s.book_fetched and not s.stale for s in rows)
    assert all(s.risk_score is not None for s in rows)
    table = format_scan_table(rows)
    assert "risk" in table.splitlines()[0]


def test_illiquid_wide_spread_filtered():
    cfg = load_config()
    client = FixtureClient("fixtures/public_markets.json")
    now = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)
    rows = scan_markets(client, cfg, now=now)
    snap = rows[0]
    snap.spread = Decimal("0.50")
    snap.best_ask = snap.best_bid + snap.spread if snap.best_bid is not None else Decimal("0.90")
    assert not is_liquid(snap, cfg.scanner)


def test_zero_volume_ok_when_depth_exists():
    cfg = load_config()
    now = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)
    snap = snapshot_from_kalshi(
        {
            "ticker": "KXLIQ-0VOL",
            "status": "open",
            "yes_bid_dollars": "0.49",
            "yes_ask_dollars": "0.51",
            "volume_fp": "0",
            "open_interest_fp": "500",
            "close_time": "2026-12-01T00:00:00Z",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.4900", "80"]],
                "no_dollars": [["0.4900", "80"]],
            }
        },
        now=now,
    )
    assert snap.volume_shares == Decimal("0")
    assert liquidity_score(snap) > 0
    assert is_liquid(snap, cfg.scanner)


def test_stale_or_unfetched_book_not_liquid():
    cfg = load_config()
    now = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)
    snap = snapshot_from_kalshi(
        {
            "ticker": "KXSTALE",
            "status": "open",
            "yes_bid_dollars": "0.49",
            "yes_ask_dollars": "0.51",
            "volume_fp": "5000",
            "close_time": "2026-12-01T00:00:00Z",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.4900", "80"]],
                "no_dollars": [["0.4900", "80"]],
            }
        },
        now=now,
    )
    snap.stale = True
    assert not is_liquid(snap, cfg.scanner)
    snap.stale = False
    snap.book_fetched = False
    assert not is_liquid(snap, cfg.scanner)


class _RankClient:
    source_name = "rank-fake"
    venue = "kalshi"

    def __init__(self) -> None:
        self.books: list[str] = []
        self._markets = [
            {
                "ticker": "KXTHIN",
                "status": "open",
                "yes_bid_dollars": "0.49",
                "yes_ask_dollars": "0.51",
                "volume_fp": "0",
                "open_interest_fp": "0",
                "liquidity_dollars": "0",
                "yes_bid_size_fp": "5",
                "yes_ask_size_fp": "5",
                "close_time": "2026-12-01T00:00:00Z",
            },
            {
                "ticker": "KXFAT",
                "status": "open",
                "yes_bid_dollars": "0.49",
                "yes_ask_dollars": "0.51",
                "volume_fp": "90000",
                "open_interest_fp": "40000",
                "liquidity_dollars": "8000",
                "yes_bid_size_fp": "400",
                "yes_ask_size_fp": "400",
                "close_time": "2026-12-01T00:00:00Z",
            },
        ]

    def list_markets(self, *, limit, active=True, closed=False, offset=0, **_k):
        return self._markets[offset : offset + limit]

    def book(self, slug: str):
        self.books.append(slug)
        return {
            "orderbook_fp": {
                "yes_dollars": [["0.4900", "80"]],
                "no_dollars": [["0.4900", "80"]],
            }
        }

    def snapshot(self, market, book=None, *, now=None):
        return snapshot_from_kalshi(market, book, now=now)

    def close(self):
        return None


def test_scanner_ranks_by_liquidity_before_book_fetch():
    cfg = load_config()
    client = _RankClient()
    now = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)
    rows = scan_markets(client, cfg, now=now)
    assert client.books[0] == "KXFAT"
    assert rows[0].slug == "KXFAT"


class _NearClient:
    source_name = "near-fake"
    venue = "kalshi"

    def __init__(self) -> None:
        self._markets = [
            {
                "ticker": "KXFAR",
                "status": "open",
                "yes_bid_dollars": "0.96",
                "yes_ask_dollars": "0.97",
                "volume_fp": "10000",
                "close_time": "2027-01-01T00:00:00Z",
                "expected_expiration_time": "2027-01-01T00:00:00Z",
            },
            {
                "ticker": "KXNEAR",
                "status": "open",
                "yes_bid_dollars": "0.96",
                "yes_ask_dollars": "0.97",
                "volume_fp": "1000",
                "close_time": "2026-10-10T00:00:00Z",
                "expected_expiration_time": "2026-09-29T10:00:00Z",
            },
        ]

    def list_markets(self, *, limit, active=True, closed=False, offset=0, **_k):
        return self._markets[offset : offset + limit]

    def book(self, slug: str):
        if slug == "FAIL":
            raise BookFetchError("nope")
        return {
            "orderbook_fp": {
                "yes_dollars": [["0.9600", "40"]],
                "no_dollars": [["0.0300", "40"]],
            }
        }

    def snapshot(self, market, book=None, *, now=None):
        return snapshot_from_kalshi(market, book, now=now)

    def close(self):
        return None


def test_near_resolution_uses_own_close_time_universe():
    cfg = load_config()
    client = _NearClient()
    now = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)
    rows = scan_near_resolution(client, cfg, now=now)
    slugs = [s.slug for s in rows]
    assert "KXNEAR" in slugs
    assert "KXFAR" not in slugs
    assert all(s.hours_to_resolution is not None and s.hours_to_resolution <= 36 for s in rows)
