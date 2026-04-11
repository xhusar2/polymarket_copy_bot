from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

log = logging.getLogger("copy_trader")

DATA_API = "https://data-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

# Substrings matched on title/slug/eventSlug/outcome (lowercased). Default filter preset.
WEATHER_KEYWORDS: tuple[str, ...] = (
    "weather",
    "temperature",
    "degrees",
    "fahrenheit",
    "celsius",
    "°f",
    "°c",
    "degree ",
    "daily high",
    "daily low",
    "high temp",
    "low temp",
    "heat index",
    "wind chill",
    "noaa",
    " nws",
    "national weather",
    "accuweather",
    "forecast high",
    "forecast low",
    "record high",
    "record low",
    "hottest",
    "coldest",
    "rainfall",
    "precipitation",
    "hurricane",
    "tornado",
    "snowfall",
    "drought",
)


def trade_fingerprint(row: dict[str, Any]) -> str:
    return "|".join(
        [
            str(row.get("transactionHash", "")),
            str(row.get("asset", "")),
            str(row.get("side", "")),
            str(row.get("size", "")),
            str(row.get("price", "")),
            str(row.get("timestamp", "")),
        ]
    )


def load_seen(path: Path, cap: int) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        seen = data.get("seen", [])
        if not isinstance(seen, list):
            return set()
        return set(seen[-cap:])
    except (json.JSONDecodeError, OSError):
        return set()


def save_seen(path: Path, seen: set[str], cap: int) -> None:
    trimmed = list(seen)[-cap:]
    path.write_text(json.dumps({"seen": trimmed}, indent=0))


def fetch_leader_trades(
    client: httpx.Client,
    user: str,
    limit: int,
    taker_only: bool,
) -> list[dict[str, Any]]:
    r = client.get(
        f"{DATA_API}/trades",
        params={
            "user": user,
            "limit": limit,
            "takerOnly": str(taker_only).lower(),
        },
    )
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        return []
    return data


@dataclass
class Settings:
    target: str
    private_key: str | None
    signature_type: int
    funder: str | None
    scale: float
    poll_interval: float
    trade_limit: int
    taker_only: bool
    dry_run: bool
    state_path: Path
    seen_cap: int
    max_buy_usd: float | None
    user_agent: str
    skip_balance_check: bool
    refresh_balance_before_sell: bool
    market_filter_mode: str
    market_filter_keywords: tuple[str, ...]


def build_clob_client(settings: Settings):
    from py_clob_client.client import ClobClient

    if not settings.private_key:
        raise SystemExit("PRIVATE_KEY is required unless DRY_RUN=1")

    temp = ClobClient(CLOB_HOST, key=settings.private_key, chain_id=CHAIN_ID)
    creds = temp.create_or_derive_api_creds()

    funder = settings.funder or temp.get_address()
    sig = settings.signature_type

    return ClobClient(
        CLOB_HOST,
        key=settings.private_key,
        chain_id=CHAIN_ID,
        creds=creds,
        signature_type=sig,
        funder=funder,
    )


def _fixed_int_to_human(raw: Any, scale: float) -> float:
    if raw is None or raw == "":
        return 0.0
    try:
        return int(raw) / scale
    except (TypeError, ValueError):
        return 0.0


def _max_allowance_human(allowances: Any, scale: float) -> float:
    if not isinstance(allowances, dict) or not allowances:
        return 0.0
    try:
        return max(int(v) for v in allowances.values()) / scale
    except (TypeError, ValueError):
        return 0.0


def trade_affordable(
    clob,
    side: str,
    token_id: str,
    amount: float,
    settings: Settings,
) -> bool:
    """
    Pre-flight using CLOB /balance-allowance (6-decimal fixed strings per API).
    BUY: collateral USDC balance + allowance vs USD notional.
    SELL: conditional token balance + allowance vs share size.
    """
    if settings.skip_balance_check:
        return True

    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

    scale = 1e6
    eps = 1e-4

    if side == "BUY":
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        resp = clob.get_balance_allowance(params)
        bal = _fixed_int_to_human(resp.get("balance"), scale)
        allow = _max_allowance_human(resp.get("allowances"), scale)
        if bal + eps < amount:
            log.warning(
                "skip BUY insufficient USDC balance need=%.4f have=%.4f",
                amount,
                bal,
            )
            return False
        if allow + eps < amount:
            log.warning(
                "skip BUY insufficient USDC allowance need=%.4f max_spender=%.4f",
                amount,
                allow,
            )
            return False
        return True

    params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
    if settings.refresh_balance_before_sell:
        try:
            clob.update_balance_allowance(params)
        except Exception as e:
            log.debug("update_balance_allowance conditional: %s", e)

    resp = clob.get_balance_allowance(params)
    bal = _fixed_int_to_human(resp.get("balance"), scale)
    raw_allow = resp.get("allowances")
    allow = _max_allowance_human(raw_allow, scale)
    if bal + eps < amount:
        log.warning(
            "skip SELL insufficient outcome balance need=%.6f have=%.6f",
            amount,
            bal,
        )
        return False
    if isinstance(raw_allow, dict) and raw_allow:
        if allow + eps < amount:
            log.warning(
                "skip SELL insufficient token allowance need=%.6f max_spender=%.6f",
                amount,
                allow,
            )
            return False
    else:
        log.debug(
            "SELL allowance map empty from API; balance-only precheck token=%s…",
            token_id[:16],
        )
    return True


def trade_matches_market_filter(trade: dict[str, Any], settings: Settings) -> bool:
    if settings.market_filter_mode == "all":
        return True
    if settings.market_filter_mode == "keywords":
        kws = settings.market_filter_keywords
    else:
        kws = WEATHER_KEYWORDS + settings.market_filter_keywords
    if not kws:
        return True
    hay = " ".join(
        [
            str(trade.get("title") or ""),
            str(trade.get("slug") or ""),
            str(trade.get("eventSlug") or ""),
            str(trade.get("outcome") or ""),
        ]
    ).lower()
    return any(k in hay for k in kws)


def mirror_trade(clob, trade: dict[str, Any], settings: Settings) -> None:
    from py_clob_client.clob_types import MarketOrderArgs, OrderType

    token_id = str(trade["asset"])
    side = str(trade["side"]).upper()
    if side not in ("BUY", "SELL"):
        log.warning("skip unknown side %s", side)
        return

    if not trade_matches_market_filter(trade, settings):
        log.info(
            "skip market filter (%s) title=%s",
            settings.market_filter_mode,
            (trade.get("title") or "")[:80],
        )
        return

    leader_size = float(trade["size"])
    leader_price = float(trade["price"])
    scaled_shares = leader_size * settings.scale

    book = clob.get_order_book(token_id)
    min_sz = float(book.min_order_size or 0)

    if side == "SELL":
        amount = scaled_shares
        if min_sz and amount < min_sz:
            log.info(
                "skip SELL below min_order_size token=%s amount=%s min=%s",
                token_id[:16],
                amount,
                min_sz,
            )
            return
    else:
        notion = leader_size * leader_price * settings.scale
        if settings.max_buy_usd is not None:
            notion = min(notion, settings.max_buy_usd)
        if min_sz:
            min_notional = min_sz * leader_price
            if notion < min_notional:
                log.info(
                    "skip BUY below min notional token=%s usd=%s min~=%s",
                    token_id[:16],
                    notion,
                    min_notional,
                )
                return
        amount = notion

    if not settings.dry_run:
        if not trade_affordable(clob, side, token_id, amount, settings):
            return

    title = trade.get("title") or ""
    log.info(
        "mirror %s %s shares~=%.4f token=%s… market=%s",
        side,
        f"${amount:.2f}" if side == "BUY" else f"{amount:.4f} sh",
        scaled_shares,
        token_id[:20],
        title[:60],
    )

    if settings.dry_run:
        return

    mo = MarketOrderArgs(
        token_id=token_id,
        amount=amount,
        side=side,
        price=0,
        order_type=OrderType.FOK,
    )
    signed = clob.create_market_order(mo)
    resp = clob.post_order(signed, orderType=OrderType.FOK)
    log.info("posted %s", resp)


def run_loop(settings: Settings) -> None:
    headers = {"User-Agent": settings.user_agent}
    http = httpx.Client(headers=headers, timeout=30.0)
    seen = load_seen(settings.state_path, settings.seen_cap)
    bootstrapped = len(seen) > 0

    clob = None
    if not settings.dry_run:
        clob = build_clob_client(settings)

    log.info(
        "copy_trader target=%s scale=%s dry_run=%s bootstrapped=%s market_filter=%s",
        settings.target,
        settings.scale,
        settings.dry_run,
        bootstrapped,
        settings.market_filter_mode,
    )

    if clob is None:
        from py_clob_client.client import ClobClient

        clob = ClobClient(CLOB_HOST, chain_id=CHAIN_ID)

    while True:
        try:
            trades = fetch_leader_trades(
                http, settings.target, settings.trade_limit, settings.taker_only
            )
            by_time = sorted(trades, key=lambda t: int(t.get("timestamp") or 0))

            if not bootstrapped:
                for t in by_time:
                    seen.add(trade_fingerprint(t))
                bootstrapped = True
                save_seen(settings.state_path, seen, settings.seen_cap)
                log.info("bootstrap: marked %d trades as seen (no copy)", len(by_time))
            else:
                for t in by_time:
                    fp = trade_fingerprint(t)
                    if fp in seen:
                        continue
                    try:
                        mirror_trade(clob, t, settings)
                    except Exception as e:
                        log.exception("mirror failed: %s", e)
                    seen.add(fp)
                save_seen(settings.state_path, seen, settings.seen_cap)

        except Exception as e:
            log.exception("poll error: %s", e)

        time.sleep(settings.poll_interval)


def settings_from_env(target_override: str | None) -> Settings:
    load_dotenv()

    target = target_override or os.environ.get("COPY_TARGET_WALLET", "").strip()
    if not target:
        print("Set COPY_TARGET_WALLET or pass --target 0x…", file=sys.stderr)
        raise SystemExit(2)

    dry = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
    pk = os.environ.get("PRIVATE_KEY", "").strip() or None
    if dry:
        pk = pk or None
    elif not pk:
        print("Set PRIVATE_KEY (or DRY_RUN=1 to observe only)", file=sys.stderr)
        raise SystemExit(2)

    sig = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "2"))
    funder = os.environ.get("POLYMARKET_FUNDER", "").strip() or None

    mf = os.environ.get("COPY_MARKET_FILTER", "weather").strip().lower()
    if mf not in ("all", "weather", "keywords"):
        log.warning("unknown COPY_MARKET_FILTER=%r; using weather", mf)
        mf = "weather"
    kw_tuple = tuple(
        k.strip().lower()
        for k in os.environ.get("COPY_MARKET_KEYWORDS", "").split(",")
        if k.strip()
    )
    if mf == "keywords" and not kw_tuple:
        print(
            "COPY_MARKET_FILTER=keywords requires non-empty COPY_MARKET_KEYWORDS",
            file=sys.stderr,
        )
        raise SystemExit(2)

    return Settings(
        target=target,
        private_key=pk,
        signature_type=sig,
        funder=funder,
        scale=float(os.environ.get("COPY_SCALE", "1")),
        poll_interval=float(os.environ.get("POLL_INTERVAL_SEC", "3")),
        trade_limit=int(os.environ.get("TRADE_POLL_LIMIT", "100")),
        taker_only=os.environ.get("TAKER_ONLY", "").lower() in ("1", "true", "yes"),
        dry_run=dry,
        state_path=Path(os.environ.get("STATE_FILE", "copy_state.json")),
        seen_cap=int(os.environ.get("SEEN_CAP", "8000")),
        max_buy_usd=float(os.environ["MAX_BUY_USD"])
        if os.environ.get("MAX_BUY_USD")
        else None,
        user_agent=os.environ.get("DATA_API_USER_AGENT", "PolymarketCopyTrader/1.0"),
        skip_balance_check=os.environ.get("SKIP_BALANCE_CHECK", "").lower()
        in ("1", "true", "yes"),
        refresh_balance_before_sell=os.environ.get(
            "REFRESH_BALANCE_BEFORE_SELL", ""
        ).lower()
        in ("1", "true", "yes"),
        market_filter_mode=mf,
        market_filter_keywords=kw_tuple,
    )
