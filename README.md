# PredictiveBot

Kalshi-primary paper-trading bot with Polymarket US as a second venue. The same scanner, fee, risk, and paper-trading code runs behind a shared exchange adapter.

**Production trading is disabled.** The bot never places, cancels, or modifies real orders on Kalshi production or Polymarket US, and never moves real funds. A separate opt-in path can post orders to the **Kalshi demo** environment only.

This is a personal research tool, not financial advice. Prediction-market trading is active, fee-sensitive, and legally messy; most retail users lose money.

## What it does

1. **Read-only scanner** — pages through Kalshi `/markets` (or Polymarket US), ranks by volume / open interest / liquidity / book depth, then fetches a small number of books. Reports mid, spread, depth, volume, hours to event. Kalshi hours use the earlier of `expected_expiration_time` and `close_time` (official close is often well after the event). Trading hours come from `GET /exchange/schedule`.
2. **Venue fee models**
   - **Kalshi** (fee schedule PDF + series `fee_type` on docs.kalshi.com): taker `round_up(M × 0.07 × C × P × (1−P))` to the next cent per order. Makers are free unless the series is `quadratic_with_maker_fees` (`0.0175`) or `quadratic_with_combo_maker_fees` (`0.035`).
   - **Polymarket US** (docs.polymarket.us/fees, effective 25 Sep 2026): taker `0.0695 × C × p × (1−p)`, maker rebate `0.0125 × C × p × (1−p)`, banker's rounding.
3. **Paper maker strategy** — spread-aware resting quotes: join the touch on a 1–2 tick book, improve by `improve_ticks` when the book is wider, inventory-skewed, never lock or cross (maker-only). Fills only on a strict trade-through or book cross-through. Kalshi last-trade is refreshed each live tick so trade-throughs can fire. Stale / unfetched books are not quoted or filled.
4. **Paper near-resolution favorites** — own universe selected by hours-to-event (not the far-dated maker scan). Resting bids on ~94–98¢ contracts. Isolated book and P&L.
5. **Risk** — per-market and gross caps, daily-loss kill switch, cancel on mid jump, resolution cutoff, and a **total account-risk cap** (default and hard max 40% of cash + MTM). At-risk is worst-case loss on open positions plus every resting order if it filled (YES buy at `p` → `p` per contract; sell/NO → `1-p`). An order that would push the total over the cap is rejected. Setting `max_account_risk_pct` above `0.40` is refused unless `allow_account_risk_above_hard_max: true`. The current risk percentage is logged and printed on the session report.
6. **Trading toggle** — `pmbot trading off` / `on` / `status` writes a small state file (AND'd with `trading.enabled` in config). Checked every loop iteration. When off, the bot cancels resting orders, stops quoting, and keeps running read-only. Only one trading process may run at a time (PID/flock lock).
7. **Cross-venue comparison** — read-only match of similar events on Kalshi and Polymarket US, with the price gap **after both venues' taker fees**. `compare.min_net_edge` is **dollars per contract**, not a dollar total on `contract_size` contracts. Alerts only; it never trades the gap. Failed book fetches are skipped (no list-price fallback).
8. **Kalshi demo session (opt-in)** — `pmbot kalshi-demo --confirm-demo` runs a quote / re-quote / cancel loop on the demo host, tracks balance / positions / fills, writes a session report, then cancel-all + verifies no resting orders remain (loud alert if any do). `pmbot kalshi-demo-order` still places a single demo order. Production URLs are refused. Signing auto-detects Ed25519 vs RSA.

## Setup

Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Keys are **environment variables**, not files in the repo. Scanning, paper trading, and the comparison report need no keys — public market data is unauthenticated on both venues.

A local `.env` file is **optional** (loaded if present) so you do not have to export vars in every shell. Never commit `.env`. `.env.example` lists names only.

## Credentials (env vars are primary)

Set these in the shell, a secrets manager, or an optional local `.env` (gitignored, never committed):

| Variable | Required for | Notes |
| --- | --- | --- |
| `KALSHI_DEMO_API_KEY_ID` | Demo orders / session | Demo key id from https://demo.kalshi.co/ |
| `KALSHI_DEMO_PRIVATE_KEY` | Demo orders / session | PEM contents (Ed25519 PKCS#8 or RSA). Preferred over a file. |
| `KALSHI_DEMO_PRIVATE_KEY_PATH` | Optional | PEM file path if you do not want the key contents in the env var. |
| `KALSHI_LIVE_TRADING` | Must stay false | Production trading is refused. |
| `POLYMARKET_LIVE_TRADING` | Must stay false | Production trading is refused. |

Collapsed / single-line PEMs are normalized before load. Do not put keys in `config.yaml` or commit them.

## Commands

Kalshi scanner against **live public production data** (no keys):

```bash
python -m polymarket_bot scan --exchange kalshi --source live
```

Same scanner on Polymarket US (`polymarket-us` SDK → `https://gateway.polymarket.us`):

```bash
python -m polymarket_bot scan --exchange polymarket_us --source live
```

Fixtures (offline):

```bash
python -m polymarket_bot scan --exchange kalshi --source fixture
python -m polymarket_bot scan --exchange polymarket_us --source fixture
```

Paper session. Default venue is Kalshi; replay fixtures produce fills without waiting on the tape:

```bash
python -m polymarket_bot paper --exchange kalshi --source replay --no-sleep --ticks 3
python -m polymarket_bot paper --exchange polymarket_us --source replay --no-sleep --ticks 3
```

Live public books, still **simulated** orders only:

```bash
python -m polymarket_bot paper --exchange kalshi --source live --ticks 4 --no-sleep
```

Cross-venue comparison (alerts only):

```bash
python -m polymarket_bot compare
```

Pause or resume order placement without editing code (state file + config flag; checked every tick):

```bash
python -m polymarket_bot trading off
python -m polymarket_bot trading status
python -m polymarket_bot trading on
```

Kalshi **demo session** (quote / re-quote / cancel, then verify no resting orders). Requires the env vars above, `kalshi.demo_orders_enabled: true`, and `--confirm-demo`. Uses demo books so tickers exist on the demo host:

```bash
python -m polymarket_bot kalshi-demo --confirm-demo --ticks 4 --no-sleep
python -m polymarket_bot kalshi-demo --confirm-demo --ticker SOME-DEMO-TICKER --ticks 4
```

Single demo order (same safety rails):

```bash
python -m polymarket_bot kalshi-demo-order --ticker SOME-TICKER --side bid --price 0.0100 --count 1 --confirm-demo
```

Disabled production path:

```bash
python -m polymarket_bot live
# always exits with an error
```

```bash
pytest
```

## Safety

- `dry_run: true`, `live_trading_enabled: false`.
- `POLYMARKET_LIVE_TRADING` and `KALSHI_LIVE_TRADING` must stay false.
- Kalshi demo orders require `kalshi.demo_orders_enabled: true`, `--confirm-demo`, and a demo host (`https://demo-api.kalshi.co/trade-api/v2`; `external-api.demo.kalshi.co` is a documented fallback). Production hosts are refused.
- Demo keys come from env vars only (see above). Never committed.
- Paper quotes never leave the process. Polymarket US has no order path at all.
- Signed demo calls retry 429 and 5xx with backoff and a fresh timestamp. Shutdown uses cancel-all (plus batch cancel if anything remains) and errors loudly if resting orders are still open.
- Failed live books are marked `stale` and are not used for quotes, simulated fills, or compare alerts.

## Data sources (verified)

### Kalshi — [docs.kalshi.com](https://docs.kalshi.com)

| Item | What we found |
| --- | --- |
| REST | Trade API v2. Prod public data `https://external-api.kalshi.com/trade-api/v2`. Demo default `https://demo-api.kalshi.co/trade-api/v2` (documented fallback: `https://external-api.demo.kalshi.co/trade-api/v2`). |
| Public data | Markets, order books, trades, events, series, `GET /exchange/schedule` — no auth. Confirmed 200 in this environment. |
| Pagination | `GET /markets` is cursor-paged (`limit` + `cursor`). The scanner walks several pages and ranks before fetching books. |
| Order book | YES bids and NO bids only. A NO bid at `p` is a YES ask at `1−p`. |
| Last trade | `GET /markets/trades?ticker=…&limit=1`. List `last_price_dollars` is often stale; live paper refreshes this each tick. |
| Auth | `KALSHI-ACCESS-KEY` / `TIMESTAMP` / `SIGNATURE`. Auto-detects Ed25519 (sign the pre-sign text directly) vs RSA-PSS SHA-256. Path includes `/trade-api/v2` and excludes the query string. Official SDKs are RSA-only. |
| Demo orders | `POST /portfolio/events/orders` (Create Order V2). Cancel-all: `DELETE /portfolio/events/orders`. Batch: `DELETE /portfolio/events/orders/batched`. |
| Rate limits | Token buckets. Basic: 200 read / 100 write tokens per second; most calls cost 10 tokens. 429 body `{"error":"too many requests"}`, **no Retry-After**. Client retries 429 and 5xx with exponential backoff. |
| Fees | Taker `round_up(0.07 × C × P × (1−P))` to the cent. Maker $0 unless the series has maker fees. |

Demo market prices may not match production. Scanner/paper default to **production public data**. The demo session uses **demo books + demo orders**. `--source kalshi-demo-data` inspects demo books from scan/paper.

### Polymarket US — [docs.polymarket.us](https://docs.polymarket.us)

| Item | What we found |
| --- | --- |
| Public REST | `https://gateway.polymarket.us` — no API key. |
| Authenticated REST | `https://api.polymarket.us` — unused. |
| Official SDK | `polymarket-us` 1.0.2. |
| Fees | Taker Θ 0.0695 / maker rebate 0.0125, banker's rounding, 25 Sep 2026. |
| Rate limit | Cloudflare 429 HTML pages are common. The bot paces requests (`min_request_interval_seconds`), fetches few books, retries 429/5xx, and **does not** quote, fill, or alert on a stale book. |

## Config

`config.yaml` is the single knob file: `exchange`, Kalshi URLs, scan pagination / ranking, quote size, risk, comparison thresholds.

- `scanner.list_page_size` / `max_list_pages` / `max_markets_to_list` — how far to page `/markets`.
- `scanner.max_book_fetches` — books fetched after ranking (keep this small on Polymarket US).
- `paper.maker.improve_ticks` — ticks to improve inside a wide book; 1–2 tick books join the touch.
- `compare.min_net_edge` — **dollars per contract** after taker fees. `compare.contract_size` is only the clip used to print dollar totals.
- `paper.risk.max_account_risk_pct` — fraction of account value that may be at risk (default/hard max `0.40`). Override only with `allow_account_risk_above_hard_max: true`.
- `trading.enabled` / `toggle_path` / `lock_path` — config master switch, CLI toggle file, and single-process lock.

## What would be needed for Kalshi or Polymarket production (not enabled)

Not implemented, not hidden behind a flag:

1. KYC'd, funded accounts on the venue you intend to trade.
2. Production API keys in env vars only (Kalshi production keys are distinct from demo keys).
3. New code that calls production order endpoints — **none of that is here**.
4. Paper results you trust, plus idempotency, reconciliation, and a real cancel-all kill switch.
5. Legal/tax review for event contracts.

Until that exists, keep `live_trading_enabled: false` and `kalshi.demo_orders_enabled` off unless you are deliberately posting to demo.
