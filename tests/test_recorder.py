import gzip
import json
from datetime import datetime, timezone
from decimal import Decimal
from polymarket_bot.config import RecordConfig, load_config
from polymarket_bot.exchanges.kalshi import snapshot_from_kalshi
from polymarket_bot.market_data import TapeTrade
from polymarket_bot.recorder import candles_from_tape, run_record, snapshot_row


class _RecordFake:
    source_name = "record-fake"
    venue = "kalshi"

    def __init__(self) -> None:
        self.books = 0
        self._market = {
            "ticker": "KXRT-FOO",
            "slug": "KXRT-FOO",
            "series_ticker": "KXRT",
            "title": "Rate",
            "status": "active",
            "yes_bid_dollars": "0.49",
            "yes_ask_dollars": "0.51",
            "close_time": "2026-12-01T00:00:00Z",
            "fee_type": "quadratic",
            "volume_fp": "1000",
            "open_interest_fp": "1000",
            "yes_bid_size_fp": "50",
            "yes_ask_size_fp": "50",
        }
        self._book = {
            "orderbook_fp": {
                "yes_dollars": [["0.4900", "50"], ["0.4800", "20"]],
                "no_dollars": [["0.4900", "50"], ["0.4800", "20"]],
            }
        }

    def list_markets(self, *, limit, active=True, closed=False, offset=0, **_k):
        return [self._market]

    def book(self, slug: str):
        self.books += 1
        return self._book

    def trades(self, slug: str, **_k):
        from polymarket_bot.market_data import TapeTrade

        return [TapeTrade(price=Decimal("0.50"), qty=Decimal("3"), trade_id="t1")]

    def snapshot(self, market, book=None, *, now=None):
        return snapshot_from_kalshi(market, book, now=now)

    def close(self):
        return None


def test_record_writes_gzip_jsonl(tmp_path):
    cfg = load_config()
    object.__setattr__(
        cfg,
        "record",
        RecordConfig(1, 2, "jsonl", tmp_path / "rec", 64, 5),
    )
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    client = _RecordFake()
    path = run_record(cfg, client, ticks=1, sleep=False, now=now)
    assert path.exists()
    lines = gzip.open(path, "rt", encoding="utf-8").read().strip().splitlines()
    assert lines
    row = json.loads(lines[0])
    assert row["ticker"] == "KXRT-FOO"
    assert len(row["bids"]) == 2
    assert row["tape"][0]["qty"] == "3"
    assert row["candles_1m"]
    assert row["candles_1m"][0]["close"] == "0.50"
    assert client.books >= 1


def test_snapshot_row_is_json_serializable():
    client = _RecordFake()
    snap = client.snapshot(client._market, client._book)
    snap.tape = client.trades("KXRT-FOO")
    row = snapshot_row(snap, now=datetime.now(timezone.utc), levels=1)
    json.dumps(row)
    assert row["bids"]
    assert "tape" in row
    assert "candles_1m" in row


def test_candles_from_tape_bucket_by_minute():
    t0 = datetime(2026, 9, 29, 12, 0, 10, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 29, 12, 0, 40, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 29, 12, 1, 5, tzinfo=timezone.utc)
    tape = [
        TapeTrade(price=Decimal("0.40"), qty=Decimal("2"), ts=t0),
        TapeTrade(price=Decimal("0.50"), qty=Decimal("1"), ts=t1),
        TapeTrade(price=Decimal("0.45"), qty=Decimal("4"), ts=t2),
    ]
    rows = candles_from_tape(tape, t2)
    assert len(rows) == 2
    assert rows[0]["open"] == "0.40"
    assert rows[0]["high"] == "0.50"
    assert rows[0]["low"] == "0.40"
    assert rows[0]["close"] == "0.50"
    assert rows[0]["volume"] == "3"
    assert rows[1]["open"] == "0.45"
    assert rows[1]["volume"] == "4"


def test_record_prefers_kalshi_candlesticks(tmp_path):
    class _WithCandles(_RecordFake):
        def candlesticks(self, slug, **_k):
            return [
                {
                    "ts": 1,
                    "open": "0.40",
                    "high": "0.55",
                    "low": "0.39",
                    "close": "0.50",
                    "volume": "12",
                }
            ]

    cfg = load_config()
    object.__setattr__(
        cfg,
        "record",
        RecordConfig(1, 2, "jsonl", tmp_path / "rec", 64, 5),
    )
    path = run_record(cfg, _WithCandles(), ticks=1, sleep=False)
    row = json.loads(gzip.open(path, "rt", encoding="utf-8").read().strip().splitlines()[0])
    assert row["tape"][0]["qty"] == "3"
    assert row["candles_1m"][0]["close"] == "0.50"
    assert row["candles_1m"][0]["volume"] == "12"
