# Polymarket copy trader

Polls a **leader** wallet’s recent fills from Polymarket’s [Data API](https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets.md), then places **FOK market orders** on the [CLOB](https://docs.polymarket.com/developers/CLOB/trades/overview) with your account via [`py-clob-client`](https://github.com/Polymarket/py-clob-client).

This is **not** official Polymarket software. Use at your own risk; prediction markets and automated trading can lose money.

## How it works

1. **Bootstrap:** On the first run (empty state file), the bot records every trade in the current API page as “seen” and **does not copy** them.
2. **Loop:** Every `POLL_INTERVAL_SEC` it fetches up to `TRADE_POLL_LIMIT` recent trades for `COPY_TARGET_WALLET`, sorts by time, and for each **new** fingerprint it runs `mirror_trade`.
3. **Mirror:** Optional **market filter** (default: weather/temperature keywords), **size** from `COPY_SCALE` and `MAX_BUY_USD`, **min size** from the book, optional **balance/allowance** pre-check, then **sign + post** an FOK market order.

Skipped trades (filter, min size, balance, errors) are still marked **seen** so they are not retried every poll.

## Requirements

- Python **3.12+** (local) or **Docker**
- A Polymarket-compatible **private key** and correct **`POLYMARKET_SIGNATURE_TYPE`** / **`POLYMARKET_FUNDER`** for your wallet type (see [Polymarket trading overview](https://docs.polymarket.com/trading/overview.md))

## Local run

```bash
cp .env.example .env   # edit: keys, leader address, DRY_RUN off for live
./run.sh               # creates .venv and runs python -m copy_trader
```

If `python3 -m venv` fails on Debian/Ubuntu: `sudo apt install python3.12-venv`.

Manual venv:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m copy_trader
```

Override leader for one run:

```bash
.venv/bin/python -m copy_trader --target 0xLeaderAddress
```

## Docker

```bash
cp .env.example .env   # configure (do not commit .env)
docker compose up --build -d
```

Compose sets **`STATE_FILE=/data/copy_state.json`** and mounts a named volume so state survives restarts.

Logs: `docker compose logs -f copy_trader`

One-off with CLI args:

```bash
docker compose run --rm copy_trader --target 0xLeaderAddress
```

## Environment variables

Copy **`.env.example`** to **`.env`** and adjust. Important entries:

| Variable | Notes |
|----------|--------|
| `COPY_TARGET_WALLET` | Leader proxy address to copy |
| `PRIVATE_KEY` | Your signer (omit or use `DRY_RUN=1` for poll-only) |
| `POLYMARKET_SIGNATURE_TYPE` | `0` EOA, `1` Magic, `2` browser/Gnosis Safe proxy (common) |
| `POLYMARKET_FUNDER` | Proxy that holds USDC if different from signer default |
| `COPY_SCALE` | Multiplier on leader size / notional |
| `MAX_BUY_USD` | Optional cap per mirrored **BUY** (USDC) |
| `COPY_MARKET_FILTER` | `weather` (default), `all`, or `keywords` (+ `COPY_MARKET_KEYWORDS`) |
| `POLL_INTERVAL_SEC` | Seconds between polls |
| `TRADE_POLL_LIMIT` | Max trades per request (API allows up to 10k; bursts can be missed if too low) |
| `TAKER_ONLY` | `true` / `1` = only taker legs; default includes maker fills |
| `DRY_RUN` | `1` = log intended orders, no signing |
| `STATE_FILE` | Path to JSON state (default `copy_state.json`) |
| `SEEN_CAP` | Max fingerprints kept in state file (default `8000`) |
| `SKIP_BALANCE_CHECK` | `1` to skip CLOB balance/allowance pre-check |
| `REFRESH_BALANCE_BEFORE_SELL` | `1` to refresh conditional balance cache before SELL check |
| `DATA_API_USER_AGENT` | Required for Data API (403 without a UA) |

## Operational notes

- **Lag:** Polling + FOK execution means you will often be slower than the leader; orders can fail if the book moves.
- **High activity:** If the leader prints more than `TRADE_POLL_LIMIT` fills between polls, older fills may never appear in the window—increase limit and/or poll more often.
- **Security:** Treat `.env` as a secret; use a dedicated hot wallet with limited funds if possible.

## License

No license file is included in this repo; clarify with the repository owner if you need one.

## References

- [Polymarket CLOB / auth / signature types](https://docs.polymarket.com/developers/CLOB/trades/overview)
- [Data API: trades by user](https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets.md)
- [`py-clob-client` (Python)](https://github.com/Polymarket/py-clob-client)
