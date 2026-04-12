# Polymarket copy trader

Polls a **leader** wallet’s recent fills from Polymarket’s [Data API](https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets.md), then places **FOK market orders** on the [CLOB](https://docs.polymarket.com/developers/CLOB/trades/overview) with your account via [`py-clob-client`](https://github.com/Polymarket/py-clob-client).

This is **not** official Polymarket software. Use at your own risk; prediction markets and automated trading can lose money.

## How it works

1. **Bootstrap:** On the first run (empty state file), the bot records every trade in the current API page as “seen” and **does not copy** them.
2. **Loop:** Every `POLL_INTERVAL_SEC` it fetches up to `TRADE_POLL_LIMIT` recent trades for `COPY_TARGET_WALLET`, sorts by time, and for each **new** fingerprint it runs `mirror_trade`.
3. **Mirror:** Optional **market filter** (default: weather/temperature keywords), **size** from `COPY_SCALE`, optional `MIN_BUY_USD` / `MAX_BUY_USD`, **min size** from the book, optional **balance/allowance** pre-check, then **sign + post** an FOK market order.

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

One-shot redeem (no copy loop; `COPY_TARGET_WALLET` optional):

```bash
.venv/bin/python -m copy_trader --redeem-once
```

### Replay last N trades once

Fetches the latest **N** rows from the Data API (default **100**), runs the same mirror path as the live loop (filters, sizing, balance check), and **merges** fingerprints into `STATE_FILE` so the next poll does not fire duplicates.

```bash
.venv/bin/python -m copy_trader --replay       # last 100
.venv/bin/python -m copy_trader --replay 50  # last 50
.venv/bin/python -m copy_trader --replay --follow   # replay then keep polling
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
docker compose run --rm copy_trader --replay 100
```

`docker compose up` does **not** pass `--replay`; use **`docker compose run`** for one-shot replay (add `--follow` to keep polling after replay).

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
| `MIN_BUY_USD` | Optional floor — skip **BUY** if notional after cap is below this (USDC) |
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
| `LOG_LEVEL` | `INFO` (default) or `DEBUG` for per-trade skip details |
| `REFRESH_BALANCE_BEFORE_BUY` | `1` = refresh collateral cache before USDC check |
| `AUTO_REDEEM` | `1` = periodically call CTF **`redeemPositions`** on Polygon for resolved markets (see below) |
| `AUTO_REDEEM_INTERVAL_SEC` | Seconds between redeem passes (default `3600`) |
| `POLYGON_RPC_URL` | HTTPS RPC for Polygon (required for on-chain redeem) |
| `REDEEM_STATE_FILE` / `REDEEM_STATE_CAP` | Track already-redeemed `conditionId`s (default `redeem_state.json`, cap `2000`) |

At startup the bot logs **`CLOB signer=`** and **`funder=`** — `funder` must match your Polymarket proxy if USDC is there; otherwise balance reads as ~0.

### Auto-redeem (resolved winners)

`AUTO_REDEEM=1` uses the [Data API `positions?redeemable=true`](https://docs.polymarket.com/api-reference/core/get-current-positions-for-a-user) for your wallet, then submits [`redeemPositions`](https://docs.polymarket.com/developers/CTF/redeem) on the Conditional Tokens contract. **Limitation:** txs are signed with **`PRIVATE_KEY`** and sent **`from` the signer address**. That only matches Polymarket **EOA** accounts (`POLYMARKET_SIGNATURE_TYPE=0`, no separate proxy funder). If **`POLYMARKET_FUNDER`** is your Polymarket **proxy**, outcome tokens sit on the proxy — this bot **cannot** auto-redeem them (would need Safe / relayer). Use **`python -m copy_trader --redeem-once`** for a manual pass; you still need **`POLYGON_RPC_URL`** and **MATIC** on the EOA for gas.

Docker: mount **`redeem_state.json`** with your state volume if you want redeem dedup to persist.

## Troubleshooting

### `invalid signature` (HTTP 400)

The CLOB rejected the signed order: the **EIP-712** payload does not match how Polymarket expects your account to trade.

1. **`PRIVATE_KEY`** — Must be the key **Polymarket issued / linked** for API trading (e.g. export from Polymarket settings for Magic, or the wallet you connected).
2. **`POLYMARKET_FUNDER`** — Must be the **proxy address** shown in the Polymarket UI (profile / deposit), not a random EOA.
3. **`POLYMARKET_SIGNATURE_TYPE`** — **`0`** = **EOA only** (signer and funder are the same address; raw private key / MetaMask account with no Polymarket proxy). **`2`** = **browser wallet + Polymarket proxy (Gnosis Safe)** — then **`POLYMARKET_FUNDER` must be the proxy** from the UI, **not** the same as the signing EOA. If **`funder == signer`** in logs and you use type **`2`**, you will usually get **`invalid signature`** → switch to **`0`** or set the real proxy as funder. **`1`** = email / Magic proxy.

Official reference: [Signature types](https://docs.polymarket.com/developers/CLOB/trades/overview#signature-types).

### `no match` (exception from `create_market_order`)

The CLOB order book has **no path to fill your FOK market order**: empty side, or **not enough displayed depth** to cover the USD (BUY) or shares (SELL) at any price. Common right after the leader trades or on thin markets. The bot logs this as a skip and continues.

## Operational notes

- **Log output:** At default **`LOG_LEVEL=INFO`** you should see **`filled BUY|SELL … order=…`** only when the CLOB **accepts** an order. If you never see `filled` but see the **`invalid signature`** warning once, wallet config is still wrong — the bot is **not** buying until that is fixed. Use **`LOG_LEVEL=DEBUG`** to see each submit attempt (`submit …`).
- **Lag:** Polling + FOK execution means you will often be slower than the leader; orders can fail if the book moves.
- **High activity:** If the leader prints more than `TRADE_POLL_LIMIT` fills between polls, older fills may never appear in the window—increase limit and/or poll more often.
- **Security:** Treat `.env` as a secret; use a dedicated hot wallet with limited funds if possible.

## License

No license file is included in this repo; clarify with the repository owner if you need one.

## References

- [Polymarket CLOB / auth / signature types](https://docs.polymarket.com/developers/CLOB/trades/overview)
- [Data API: trades by user](https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets.md)
- [`py-clob-client` (Python)](https://github.com/Polymarket/py-clob-client)
