from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import httpx
from dotenv import load_dotenv
from py_clob_client.exceptions import PolyApiException

log = logging.getLogger("copy_trader")

# One-time hint when CLOB reports ~0 USDC collateral (usually wrong POLYMARKET_FUNDER).
_logged_usdc_zero_hint = False
# One-time hint for HTTP 400 invalid signature on post_order.
_invalid_signature_hint = False
# One-time hint for HTTP 400 insufficient balance / allowance on post_order.
_insufficient_balance_hint = False
# One-time hint for HTTP 403 CLOB regional geoblock on post_order.
_geoblock_hint = False
_auto_redeem_no_rpc_logged = False

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


def _poly_error_text(exc: PolyApiException) -> str:
    m = exc.error_msg
    if isinstance(m, dict):
        return str(m.get("error", m))
    return str(m)


def _is_invalid_signature_error(exc: BaseException) -> bool:
    if isinstance(exc, PolyApiException):
        return "invalid signature" in _poly_error_text(exc).lower()
    return "invalid signature" in str(exc).lower()


def _is_insufficient_balance_error(exc: BaseException) -> bool:
    if isinstance(exc, PolyApiException):
        t = _poly_error_text(exc).lower()
    else:
        t = str(exc).lower()
    return "not enough balance" in t or (
        "insufficient" in t and ("balance" in t or "allowance" in t)
    )


def _is_geoblock_error(exc: BaseException) -> bool:
    if isinstance(exc, PolyApiException) and exc.status_code == 403:
        t = _poly_error_text(exc).lower()
        return (
            "region" in t
            or "geoblock" in t
            or "restricted" in t
            or "trading restricted" in t
        )
    s = str(exc).lower()
    return "trading restricted" in s and "region" in s


def _log_geoblock_hint_once() -> None:
    global _geoblock_hint
    if _geoblock_hint:
        return
    _geoblock_hint = True
    log.warning(
        "CLOB 403 — trading geoblocked for your region/IP. Orders will not post until you use an "
        "allowed network (see https://docs.polymarket.com/developers/CLOB/geoblock). "
        "Further post_order failures log at DEBUG only."
    )


def _is_no_orderbook_error(exc: BaseException) -> bool:
    """CLOB has no book for this outcome token (resolved/closed/unsupported on CLOB)."""
    if isinstance(exc, PolyApiException):
        t = _poly_error_text(exc).lower()
        if "no orderbook" in t:
            return True
        # Some deployments return 404 with a generic body
        return exc.status_code == 404 and "token" in t
    return "no orderbook" in str(exc).lower()


def _log_insufficient_balance_hint_once(settings: Settings) -> None:
    global _insufficient_balance_hint
    if _insufficient_balance_hint:
        return
    _insufficient_balance_hint = True
    extra = (
        " SKIP_BALANCE_CHECK=1 bypassed preflight — turn it off to skip orders before post."
        if settings.skip_balance_check
        else ""
    )
    log.warning(
        "CLOB 'not enough balance / allowance' — collateral for this signer/funder is 0 on-chain "
        "or allowance missing. Deposit USDC to Polymarket for this account; set POLYMARKET_FUNDER to "
        "the wallet that actually holds USDC; approve USDC for the CLOB in the UI if needed.%s",
        extra,
    )


def _validate_evm_address(env_name: str, raw: str) -> str:
    """Validate a single 0x + 40-hex EVM address; return normalized lowercase hex."""
    s = raw.strip()
    if not s:
        raise SystemExit(f"{env_name}: empty address")
    if not s.startswith("0x"):
        raise SystemExit(f"{env_name} must start with 0x (got {s[:24]}…)")
    body = s[2:]
    if len(body) != len(body.encode("ascii")):
        raise SystemExit(f"{env_name} must be ASCII hex")
    if not all(c in "0123456789abcdefABCDEF" for c in body):
        raise SystemExit(f"{env_name} must be hex digits only (after 0x)")
    if len(body) == 64:
        raise SystemExit(
            f"{env_name} has 64 hex characters after 0x — that is a **private key** length. "
            "Use a 20-byte wallet address (40 hex chars), not PRIVATE_KEY."
        )
    if len(body) != 40:
        raise SystemExit(
            f"{env_name} must be an EVM address: exactly 40 hex characters after 0x "
            f"(got {len(body)}). Example: 0xabc…def (42 chars total)."
        )
    return "0x" + body.lower()


def _parse_optional_evm_address(env_name: str, raw: str | None) -> str | None:
    """Return normalized 0x-prefixed lowercase address, or None if unset."""
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    return _validate_evm_address(env_name, s)


def _parse_target_wallets(env_name: str, raw: str) -> tuple[str, ...]:
    """Comma- or newline-separated leader addresses; dedupe preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for part in raw.replace("\n", ",").split(","):
        p = part.strip()
        if not p:
            continue
        addr = _validate_evm_address(env_name, p)
        lk = addr.lower()
        if lk not in seen:
            seen.add(lk)
            out.append(addr)
    if not out:
        raise SystemExit(f"{env_name}: at least one 0x address required")
    return tuple(out)


def _flatten_cli_targets(targets_cli: list[str] | None) -> str | None:
    if not targets_cli:
        return None
    parts: list[str] = []
    for item in targets_cli:
        for seg in item.replace("\n", ",").split(","):
            s = seg.strip()
            if s:
                parts.append(s)
    return ",".join(parts) if parts else None


def _log_invalid_signature_hint_once() -> None:
    global _invalid_signature_hint
    if _invalid_signature_hint:
        return
    _invalid_signature_hint = True
    log.warning(
        "CLOB 'invalid signature' — EIP-712 maker does not match this account model. "
        "(1) PRIVATE_KEY: use the key exported from https://polymarket.com/settings if you use email/Magic. "
        "(2) Types 1 (Magic) & 2 (browser): POLYMARKET_FUNDER must be the **proxy** in Profile — not the EOA. "
        "Type 0 (EOA): funder must equal signer; no proxy. "
        "(3) Try POLYMARKET_SIGNATURE_TYPE 1 vs 2 vs 0 to match how you created the account. "
        "See https://docs.polymarket.com/developers/CLOB/trades/overview#signature-types"
    )


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


def fetch_leader_trades_multi(
    client: httpx.Client,
    users: Sequence[str],
    limit: int,
    taker_only: bool,
) -> list[dict[str, Any]]:
    """Fetch up to `limit` trades per leader; merge (caller sorts / dedupes by fingerprint)."""
    merged: list[dict[str, Any]] = []
    for user in users:
        merged.extend(fetch_leader_trades(client, user, limit, taker_only))
    return merged


@dataclass
class Settings:
    targets: tuple[str, ...]
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
    min_buy_usd: float | None
    min_copy_price: float | None
    max_copy_price: float | None
    user_agent: str
    skip_balance_check: bool
    refresh_balance_before_buy: bool
    refresh_balance_before_sell: bool
    market_filter_mode: str
    market_filter_keywords: tuple[str, ...]
    auto_redeem: bool
    auto_redeem_interval_sec: float
    polygon_rpc_url: str | None
    redeem_state_path: Path
    redeem_state_cap: int
    poly_builder_key: str | None
    poly_builder_secret: str | None
    poly_builder_passphrase: str | None
    relayer_url: str | None
    market_order_type: str  # FOK | FAK


def _resolve_market_order_type(settings: Settings):
    """CLOB market order execution: FOK = all-or-nothing, FAK = fill available then cancel rest."""
    from py_clob_client.clob_types import OrderType

    name = (settings.market_order_type or "FOK").strip().upper()
    if name == "FAK":
        return OrderType.FAK
    return OrderType.FOK


def _has_builder_relayer_creds(settings: Settings) -> bool:
    return bool(
        settings.poly_builder_key
        and settings.poly_builder_secret
        and settings.poly_builder_passphrase
    )


def build_clob_client(settings: Settings):
    from py_clob_client.client import ClobClient

    if not settings.private_key:
        raise SystemExit("PRIVATE_KEY is required unless DRY_RUN=1")

    temp = ClobClient(CLOB_HOST, key=settings.private_key, chain_id=CHAIN_ID)
    creds = temp.create_or_derive_api_creds()

    signer_addr = temp.get_address()
    funder = settings.funder or signer_addr
    sig = settings.signature_type

    if sig in (1, 2) and funder.lower() == signer_addr.lower():
        log.warning(
            "POLYMARKET_SIGNATURE_TYPE=%s (Polymarket proxy account: Magic=1, browser=2) but "
            "funder equals signer EOA — this usually causes CLOB 'invalid signature'. "
            "Set POLYMARKET_FUNDER to the **proxy wallet** from the Polymarket profile dropdown "
            "(where your USDC sits), not the exported EOA. "
            "Use POLYMARKET_SIGNATURE_TYPE=0 only for a standalone EOA with no Polymarket proxy.",
            sig,
        )
    if sig == 0 and funder.lower() != signer_addr.lower():
        log.warning(
            "POLYMARKET_SIGNATURE_TYPE=0 (EOA) but funder (%s) != signer (%s) — "
            "this causes CLOB 'invalid signature'. For type 0, unset POLYMARKET_FUNDER or set it to the "
            "signer address. If USDC is on a Polymarket proxy, use POLYMARKET_SIGNATURE_TYPE=2 and "
            "POLYMARKET_FUNDER=proxy.",
            funder,
            signer_addr,
        )

    client = ClobClient(
        CLOB_HOST,
        key=settings.private_key,
        chain_id=CHAIN_ID,
        creds=creds,
        signature_type=sig,
        funder=funder,
    )
    log.info(
        "CLOB signer=%s funder=%s POLYMARKET_SIGNATURE_TYPE=%s",
        client.get_address(),
        funder,
        sig,
    )
    return client


def clob_identity_check(settings: Settings) -> int:
    """
    Print signer/funder and CLOB-reported USDC collateral (same path as the bot).
    Use this to verify PRIVATE_KEY, POLYMARKET_FUNDER, and POLYMARKET_SIGNATURE_TYPE
    match what Polymarket's API sees — independent of on-chain explorers or the website
    if the funder address differs.
    """
    if settings.dry_run:
        print(
            "DRY_RUN=1 — unset DRY_RUN and set PRIVATE_KEY to query the CLOB.",
            file=sys.stderr,
        )
        return 2
    if not settings.private_key:
        print("PRIVATE_KEY is required for --check.", file=sys.stderr)
        return 2

    clob = build_clob_client(settings)
    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    refreshed = False
    try:
        clob.update_balance_allowance(params)
        refreshed = True
    except Exception as e:
        print(f"update_balance_allowance: {e}", file=sys.stderr)

    resp = clob.get_balance_allowance(params)
    scale = 1e6
    bal_raw = resp.get("balance")
    allowances = resp.get("allowances")
    bal = _fixed_int_to_human(bal_raw, scale)
    allow_h = _max_allowance_human(allowances, scale)

    signer = clob.get_address()
    resolved_funder = settings.funder if settings.funder else signer

    print("--- Polymarket CLOB (same as bot) ---")
    print(f"signer from PRIVATE_KEY:     {signer}")
    print(f"funder (collateral wallet): {resolved_funder}")
    print(f"POLYMARKET_SIGNATURE_TYPE:  {settings.signature_type}")
    print(f"SKIP_BALANCE_CHECK:         {settings.skip_balance_check}")
    print(f"cache refresh attempted:    {refreshed}")
    print(f"USDC balance (CLOB):        {bal:.6f}  (raw balance field: {bal_raw!r})")
    print(f"allowances (raw):           {allowances!r}")
    print(f"max spender allowance ~:    {allow_h:.6f}")
    if bal < 1e-6:
        print(
            "\nCLOB sees ~0 USDC. If the website shows funds, compare Profile/deposit address "
            "to `funder` above — they must match for API orders. Try POLYMARKET_SIGNATURE_TYPE "
            "0 (EOA, funder=signer) vs 1/2 (proxy as funder) until this balance matches expectations."
        )
    elif settings.signature_type in (1, 2) and resolved_funder.lower() == signer.lower():
        print(
            "\nFor signature types 1 & 2, `funder` should be your Polymarket **proxy** (profile), "
            "usually different from the exported key's EOA — otherwise orders often fail with "
            "'invalid signature' even if balance looks fine here."
        )
    return 0


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
    global _logged_usdc_zero_hint

    if settings.skip_balance_check:
        return True

    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

    scale = 1e6
    eps = 1e-4

    if side == "BUY":
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        if settings.refresh_balance_before_buy:
            try:
                clob.update_balance_allowance(params)
            except Exception as e:
                log.debug("update_balance_allowance collateral: %s", e)
        resp = clob.get_balance_allowance(params)
        bal = _fixed_int_to_human(resp.get("balance"), scale)
        allow = _max_allowance_human(resp.get("allowances"), scale)
        if bal + eps < amount:
            if not _logged_usdc_zero_hint and bal <= eps:
                _logged_usdc_zero_hint = True
                log.warning(
                    "CLOB reports ~0 USDC collateral for signer/funder above — "
                    "set POLYMARKET_FUNDER to your Polymarket proxy if funds are there; "
                    "per-trade balance skips log at DEBUG"
                )
            log.debug(
                "skip BUY insufficient USDC balance need=%.4f have=%.4f",
                amount,
                bal,
            )
            return False
        if allow + eps < amount:
            log.debug(
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
        log.debug(
            "skip SELL insufficient outcome balance need=%.6f have=%.6f",
            amount,
            bal,
        )
        return False
    if isinstance(raw_allow, dict) and raw_allow:
        if allow + eps < amount:
            log.debug(
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
    from py_clob_client.clob_types import MarketOrderArgs

    token_id = str(trade["asset"])
    side = str(trade["side"]).upper()
    if side not in ("BUY", "SELL"):
        log.warning("skip unknown side %s", side)
        return

    if not trade_matches_market_filter(trade, settings):
        log.debug(
            "skip market filter (%s) title=%s",
            settings.market_filter_mode,
            (trade.get("title") or "")[:80],
        )
        return

    leader_size = float(trade["size"])
    leader_price = float(trade["price"])
    if side == "BUY":
        if (
            settings.min_copy_price is not None
            and leader_price < settings.min_copy_price - 1e-12
        ):
            log.debug(
                "skip BUY below MIN_COPY_PRICE token=%s price=%.4f min=%.4f",
                token_id[:16],
                leader_price,
                settings.min_copy_price,
            )
            return
        if (
            settings.max_copy_price is not None
            and leader_price > settings.max_copy_price + 1e-12
        ):
            log.debug(
                "skip BUY above MAX_COPY_PRICE token=%s price=%.4f max=%.4f",
                token_id[:16],
                leader_price,
                settings.max_copy_price,
            )
            return
    scaled_shares = leader_size * settings.scale

    try:
        book = clob.get_order_book(token_id)
    except PolyApiException as e:
        if _is_no_orderbook_error(e):
            log.debug(
                "skip %s: no CLOB orderbook token=%s… — %s",
                side,
                token_id[:20],
                _poly_error_text(e)[:120],
            )
            return
        raise
    min_sz = float(book.min_order_size or 0)

    if side == "SELL":
        amount = scaled_shares
        if min_sz and amount < min_sz:
            log.debug(
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
        if settings.min_buy_usd is not None and notion < settings.min_buy_usd - 1e-9:
            log.debug(
                "skip BUY below MIN_BUY_USD token=%s usd=%.4f min=%s",
                token_id[:16],
                notion,
                settings.min_buy_usd,
            )
            return
        if min_sz:
            min_notional = min_sz * leader_price
            if notion < min_notional:
                log.debug(
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
    log.debug(
        "submit %s %s shares~=%.4f token=%s… | %s",
        side,
        f"${amount:.2f}" if side == "BUY" else f"{amount:.4f} sh",
        scaled_shares,
        token_id[:20],
        (title or "")[:60],
    )

    if settings.dry_run:
        log.info(
            "[dry_run] would %s %s (%s) | %s",
            side,
            f"${amount:.2f}" if side == "BUY" else f"{amount:.4f} sh",
            settings.market_order_type,
            (title or "")[:50],
        )
        return

    ot = _resolve_market_order_type(settings)
    mo = MarketOrderArgs(
        token_id=token_id,
        amount=amount,
        side=side,
        price=0,
        order_type=ot,
    )
    try:
        signed = clob.create_market_order(mo)
        resp = clob.post_order(signed, orderType=ot)
        oid = resp.get("orderID") if isinstance(resp, dict) else resp
        amt_s = f"${amount:.2f}" if side == "BUY" else f"{amount:.4f} sh"
        if settings.market_order_type.upper() == "FAK":
            log.info(
                "filled %s %s (FAK target; check UI for actual size) | order=%s | %s",
                side,
                amt_s,
                oid,
                (title or "")[:55],
            )
        else:
            log.info(
                "filled %s %s | order=%s | %s",
                side,
                amt_s,
                oid,
                (title or "")[:55],
            )
        log.debug("posted %s", resp)
    except PolyApiException as e:
        if _is_invalid_signature_error(e):
            _log_invalid_signature_hint_once()
            log.debug("post_order: %s", e)
            return
        if _is_insufficient_balance_error(e):
            _log_insufficient_balance_hint_once(settings)
            log.debug(
                "skip %s insufficient balance/allowance post_order: %s",
                side,
                _poly_error_text(e) if isinstance(e, PolyApiException) else e,
            )
            return
        if _is_geoblock_error(e):
            _log_geoblock_hint_once()
            log.debug("skip %s post_order geoblocked: %s", side, _poly_error_text(e))
            return
        raise
    except Exception as e:
        # py-clob-client: empty book or not enough ask depth to fill FOK BUY/SELL notional
        if str(e) == "no match":
            log.debug(
                "skip %s %s: no book / no liquidity to price market order token=%s…",
                side,
                settings.market_order_type,
                token_id[:20],
            )
            return
        raise


def redeem_winnings_once(settings: Settings) -> None:
    """Single pass: redeemable positions via relayer (Builder creds) or Polygon RPC (EOA)."""
    if not settings.private_key:
        raise SystemExit("PRIVATE_KEY required for --redeem-once")
    if (
        not settings.polygon_rpc_url
        and not settings.dry_run
        and not _has_builder_relayer_creds(settings)
    ):
        raise SystemExit(
            "Set POLYGON_RPC_URL for RPC redeem, or POLY_BUILDER_API_KEY+POLY_BUILDER_SECRET+"
            "POLY_BUILDER_PASSPHRASE for gasless relayer (or DRY_RUN=1)"
        )
    headers = {"User-Agent": settings.user_agent}
    http = httpx.Client(headers=headers, timeout=30.0)
    try:
        from .redeem import redeem_winnings_pass

        redeem_winnings_pass(
            private_key=settings.private_key,
            funder=settings.funder,
            user_agent=settings.user_agent,
            dry_run=settings.dry_run,
            polygon_rpc_url=settings.polygon_rpc_url or "",
            redeem_state_path=settings.redeem_state_path,
            redeemed_cap=settings.redeem_state_cap,
            http=http,
            poly_builder_key=settings.poly_builder_key,
            poly_builder_secret=settings.poly_builder_secret,
            poly_builder_passphrase=settings.poly_builder_passphrase,
            relayer_url=settings.relayer_url,
        )
    finally:
        http.close()


def run_loop(settings: Settings) -> None:
    global _auto_redeem_no_rpc_logged

    headers = {"User-Agent": settings.user_agent}
    http = httpx.Client(headers=headers, timeout=30.0)
    seen = load_seen(settings.state_path, settings.seen_cap)
    bootstrapped = len(seen) > 0

    clob = None
    if not settings.dry_run:
        clob = build_clob_client(settings)

    log.info(
        "copy_trader targets=%d [%s…] scale=%s order_type=%s dry_run=%s bootstrapped=%s market_filter=%s auto_redeem=%s",
        len(settings.targets),
        ",".join(t[:10] for t in settings.targets),
        settings.scale,
        settings.market_order_type,
        settings.dry_run,
        bootstrapped,
        settings.market_filter_mode,
        settings.auto_redeem,
    )

    if clob is None:
        from py_clob_client.client import ClobClient

        clob = ClobClient(CLOB_HOST, chain_id=CHAIN_ID)

    last_auto_redeem = 0.0

    while True:
        try:
            trades = fetch_leader_trades_multi(
                http, settings.targets, settings.trade_limit, settings.taker_only
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
                    except PolyApiException as e:
                        log.exception("mirror failed: %s", e)
                    except Exception as e:
                        log.exception("mirror failed: %s", e)
                    seen.add(fp)
                save_seen(settings.state_path, seen, settings.seen_cap)

        except Exception as e:
            log.exception("poll error: %s", e)

        if settings.auto_redeem:
            now = time.time()
            if now - last_auto_redeem >= settings.auto_redeem_interval_sec:
                last_auto_redeem = now
                if not settings.polygon_rpc_url and not _has_builder_relayer_creds(
                    settings
                ):
                    if not _auto_redeem_no_rpc_logged:
                        _auto_redeem_no_rpc_logged = True
                        log.warning(
                            "AUTO_REDEEM=1 but neither POLYGON_RPC_URL nor Builder relayer "
                            "creds (POLY_BUILDER_API_KEY, POLY_BUILDER_SECRET, POLY_BUILDER_PASSPHRASE)"
                        )
                elif settings.private_key:
                    try:
                        from .redeem import redeem_winnings_pass

                        redeem_winnings_pass(
                            private_key=settings.private_key,
                            funder=settings.funder,
                            user_agent=settings.user_agent,
                            dry_run=settings.dry_run,
                            polygon_rpc_url=settings.polygon_rpc_url or "",
                            redeem_state_path=settings.redeem_state_path,
                            redeemed_cap=settings.redeem_state_cap,
                            http=http,
                            poly_builder_key=settings.poly_builder_key,
                            poly_builder_secret=settings.poly_builder_secret,
                            poly_builder_passphrase=settings.poly_builder_passphrase,
                            relayer_url=settings.relayer_url,
                        )
                    except Exception as e:
                        log.warning("auto_redeem pass failed: %s", e)

        time.sleep(settings.poll_interval)


def replay_last_trades(settings: Settings, limit: int) -> None:
    """
    Fetch up to `limit` recent leader trades and run mirror_trade for each (oldest first).
    Fingerprints are merged into seen so the main loop will not duplicate them immediately.
    """
    if limit < 1:
        raise SystemExit("--replay N requires N >= 1")
    if limit > 10000:
        log.warning("capping replay at 10000 (Data API max)")
        limit = 10000

    headers = {"User-Agent": settings.user_agent}
    http = httpx.Client(headers=headers, timeout=30.0)
    seen = load_seen(settings.state_path, settings.seen_cap)

    clob = None
    if not settings.dry_run:
        clob = build_clob_client(settings)
    if clob is None:
        from py_clob_client.client import ClobClient

        clob = ClobClient(CLOB_HOST, chain_id=CHAIN_ID)

    trades = fetch_leader_trades_multi(
        http, settings.targets, limit, settings.taker_only
    )
    by_time = sorted(trades, key=lambda t: int(t.get("timestamp") or 0))

    log.info(
        "replay: mirroring %d trade(s) for %d leader(s) (dry_run=%s)",
        len(by_time),
        len(settings.targets),
        settings.dry_run,
    )

    for t in by_time:
        fp = trade_fingerprint(t)
        try:
            mirror_trade(clob, t, settings)
        except Exception as e:
            log.exception("replay mirror failed: %s", e)
        seen.add(fp)

    save_seen(settings.state_path, seen, settings.seen_cap)
    log.info("replay: done; state saved (%d fingerprints in store)", len(seen))


def _parse_market_order_type_env() -> str:
    raw = os.environ.get("COPY_ORDER_TYPE", "FOK").strip().upper()
    if raw not in ("FOK", "FAK"):
        log.warning("unknown COPY_ORDER_TYPE=%r — use FOK or FAK; defaulting to FOK", raw)
        return "FOK"
    return raw


def _parse_price_threshold_env(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a float between 0 and 1")
    if not math.isfinite(v) or v < 0 or v > 1:
        raise SystemExit(f"{name} must be between 0 and 1")
    return v


def settings_from_env(
    targets_cli: list[str] | None = None,
    *,
    require_copy_target: bool = True,
) -> Settings:
    load_dotenv()

    cli_raw = _flatten_cli_targets(targets_cli)
    env_raw = os.environ.get("COPY_TARGET_WALLET", "").strip()

    if cli_raw:
        targets = _parse_target_wallets("COPY_TARGET_WALLET (--target)", cli_raw)
    elif env_raw:
        targets = _parse_target_wallets("COPY_TARGET_WALLET", env_raw)
    elif require_copy_target:
        print(
            "Set COPY_TARGET_WALLET (comma-separated 0x…) or pass --target (repeat or CSV)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    else:
        targets = ("0x0000000000000000000000000000000000000001",)

    dry = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
    pk = os.environ.get("PRIVATE_KEY", "").strip() or None
    if dry:
        pk = pk or None
    elif not pk:
        print("Set PRIVATE_KEY (or DRY_RUN=1 to observe only)", file=sys.stderr)
        raise SystemExit(2)

    sig = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "2"))
    funder = _parse_optional_evm_address(
        "POLYMARKET_FUNDER", os.environ.get("POLYMARKET_FUNDER", "").strip() or None
    )

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

    max_buy = (
        float(os.environ["MAX_BUY_USD"]) if os.environ.get("MAX_BUY_USD") else None
    )
    min_buy = (
        float(os.environ["MIN_BUY_USD"]) if os.environ.get("MIN_BUY_USD") else None
    )
    min_copy_price = _parse_price_threshold_env("MIN_COPY_PRICE")
    max_copy_price = _parse_price_threshold_env("MAX_COPY_PRICE")
    if (
        max_buy is not None
        and min_buy is not None
        and min_buy > max_buy + 1e-9
    ):
        log.warning(
            "MIN_BUY_USD (%s) > MAX_BUY_USD (%s) — every BUY will be skipped",
            min_buy,
            max_buy,
        )
    if (
        min_copy_price is not None
        and max_copy_price is not None
        and min_copy_price > max_copy_price + 1e-12
    ):
        raise SystemExit(
            "MIN_COPY_PRICE cannot be greater than MAX_COPY_PRICE "
            "(both are in [0, 1])"
        )

    return Settings(
        targets=targets,
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
        max_buy_usd=max_buy,
        min_buy_usd=min_buy,
        min_copy_price=min_copy_price,
        max_copy_price=max_copy_price,
        user_agent=os.environ.get("DATA_API_USER_AGENT", "PolymarketCopyTrader/1.0"),
        skip_balance_check=os.environ.get("SKIP_BALANCE_CHECK", "").lower()
        in ("1", "true", "yes"),
        refresh_balance_before_buy=os.environ.get(
            "REFRESH_BALANCE_BEFORE_BUY", ""
        ).lower()
        in ("1", "true", "yes"),
        refresh_balance_before_sell=os.environ.get(
            "REFRESH_BALANCE_BEFORE_SELL", ""
        ).lower()
        in ("1", "true", "yes"),
        market_filter_mode=mf,
        market_filter_keywords=kw_tuple,
        auto_redeem=os.environ.get("AUTO_REDEEM", "").lower()
        in ("1", "true", "yes"),
        auto_redeem_interval_sec=float(
            os.environ.get("AUTO_REDEEM_INTERVAL_SEC", "3600")
        ),
        polygon_rpc_url=os.environ.get("POLYGON_RPC_URL", "").strip() or None,
        redeem_state_path=Path(
            os.environ.get("REDEEM_STATE_FILE", "redeem_state.json")
        ),
        redeem_state_cap=int(os.environ.get("REDEEM_STATE_CAP", "2000")),
        poly_builder_key=os.environ.get("POLY_BUILDER_API_KEY", "").strip() or None,
        poly_builder_secret=os.environ.get("POLY_BUILDER_SECRET", "").strip() or None,
        poly_builder_passphrase=os.environ.get(
            "POLY_BUILDER_PASSPHRASE", ""
        ).strip()
        or None,
        relayer_url=os.environ.get("RELAYER_URL", "").strip() or None,
        market_order_type=_parse_market_order_type_env(),
    )
