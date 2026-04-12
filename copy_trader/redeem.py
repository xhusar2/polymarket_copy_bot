"""
CTF redeemPositions for resolved Polymarket markets.

Two execution paths:
1. **Builder relayer** (gasless): `POLY_BUILDER_API_KEY` + secret + passphrase → Polymarket
   `RelayClient` executes via your **Gnosis Safe** (CREATE2-derived from the EOA).
   Requires the Safe to be **deployed** (normal Polymarket browser flow). See
   https://docs.polymarket.com/trading/gasless
2. **Raw Polygon RPC**: `POLYGON_RPC_URL` + same EOA as token holder (`POLYMARKET_FUNDER`
   unset or equals signer). You pay MATIC gas.

If `POLYMARKET_FUNDER` is set and is **not** that derived Safe address, neither path
applies (Magic-only proxy, etc.) — redeem in the UI.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
from eth_account import Account
from web3 import Web3

log = logging.getLogger("copy_trader")

DATA_API = "https://data-api.polymarket.com"
CHAIN_ID = 137
DEFAULT_RELAYER_URL = "https://relayer-v2.polymarket.com"
USDC_E = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
CTF = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
INDEX_SETS_BINARY = [1, 2]

REDEEM_ABI = [
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

_logged_proxy_skip = False
_logged_relayer_funder_mismatch = False
_logged_relayer_not_deployed = False


def _log_proxy_skip_once() -> None:
    global _logged_proxy_skip
    if _logged_proxy_skip:
        return
    _logged_proxy_skip = True
    log.warning(
        "AUTO_REDEEM: tokens are on POLYMARKET_FUNDER != signer. "
        "Use Polymarket UI, or set POLY_BUILDER_* + relayer if funder is your "
        "CREATE2 Safe derived from this PRIVATE_KEY (see gasless docs)."
    )


def _expected_safe_address(private_key: str) -> str:
    from py_builder_relayer_client.builder.derive import derive
    from py_builder_relayer_client.config import get_contract_config

    acct = Account.from_key(private_key)
    cfg = get_contract_config(CHAIN_ID)
    return derive(acct.address, cfg.safe_factory)


def _encode_redeem_calldata(condition_id: str) -> str:
    w3 = Web3()
    ctf = w3.eth.contract(address=CTF, abi=REDEEM_ABI)
    cond_bytes = Web3.to_bytes(hexstr=condition_id)
    return ctf.encode_abi(
        "redeemPositions",
        [USDC_E, bytes(32), cond_bytes, INDEX_SETS_BINARY],
    )


def load_redeemed(path: Path, cap: int) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        redeemed = data.get("redeemed", [])
        if not isinstance(redeemed, list):
            return set()
        return set(str(x).lower() for x in redeemed[-cap:])
    except (json.JSONDecodeError, OSError):
        return set()


def save_redeemed(path: Path, redeemed: set[str], cap: int) -> None:
    trimmed = sorted(redeemed)[-cap:]
    path.write_text(json.dumps({"redeemed": trimmed}, indent=0))


def fetch_redeemable_positions(
    http: httpx.Client,
    wallet: str,
    user_agent: str,
) -> list[dict[str, Any]]:
    r = http.get(
        f"{DATA_API}/positions",
        params={
            "user": wallet,
            "redeemable": "true",
            "limit": 500,
        },
        headers={"User-Agent": user_agent},
        timeout=60.0,
    )
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def _condition_ids_from_positions(rows: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for row in rows:
        if not row.get("redeemable", False):
            continue
        cid = row.get("conditionId")
        if not cid or not isinstance(cid, str):
            continue
        key = cid.lower()
        if key not in seen:
            seen.add(key)
            out.append(cid)
    return out


def _builder_creds_ok(key: str | None, secret: str | None, passphrase: str | None) -> bool:
    return bool(key and secret and passphrase)


def _relayer_positions_wallet(
    private_key: str,
    funder: str | None,
) -> str | None:
    global _logged_relayer_funder_mismatch
    safe = Web3.to_checksum_address(_expected_safe_address(private_key))
    if not funder:
        return safe
    fu = Web3.to_checksum_address(funder)
    if fu.lower() != safe.lower():
        if not _logged_relayer_funder_mismatch:
            _logged_relayer_funder_mismatch = True
            log.warning(
                "AUTO_REDEEM relayer: POLYMARKET_FUNDER (%s) != Safe derived from key (%s). "
                "Relayer only executes through that Safe — skipping relayer redeem.",
                fu,
                safe,
            )
        return None
    return fu


def _redeem_via_relayer(
    private_key: str,
    relayer_url: str,
    builder_key: str,
    builder_secret: str,
    builder_passphrase: str,
    condition_ids: list[str],
    redeemed: set[str],
    redeem_state_path: Path,
    redeemed_cap: int,
) -> None:
    global _logged_relayer_not_deployed
    from py_builder_relayer_client.client import RelayClient
    from py_builder_relayer_client.models import OperationType, SafeTransaction
    from py_builder_signing_sdk.config import BuilderConfig
    from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds

    builder_config = BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(
            key=builder_key,
            secret=builder_secret,
            passphrase=builder_passphrase,
        )
    )
    client = RelayClient(relayer_url, CHAIN_ID, private_key, builder_config)
    safe_addr = client.get_expected_safe()
    if not client.get_deployed(safe_addr):
        if not _logged_relayer_not_deployed:
            _logged_relayer_not_deployed = True
            log.warning(
                "AUTO_REDEEM relayer: Safe %s is not deployed yet. "
                "Use Polymarket once with this wallet or run relayer deploy() — skipping.",
                safe_addr,
            )
        return

    for cid in condition_ids:
        try:
            data = _encode_redeem_calldata(cid)
        except Exception as e:
            log.warning("auto_redeem relayer: bad conditionId %s…: %s", cid[:16], e)
            continue

        tx = SafeTransaction(
            to=CTF,
            operation=OperationType.Call,
            data=data,
            value="0",
        )
        try:
            resp = client.execute([tx], f"Redeem {cid[:12]}…")
            result = resp.wait()
        except Exception as e:
            log.warning("auto_redeem relayer: failed conditionId=%s…: %s", cid[:18], e)
            continue

        if result is None:
            log.warning("auto_redeem relayer: timeout/fail conditionId=%s…", cid[:18])
            continue

        redeemed.add(cid.lower())
        save_redeemed(redeem_state_path, redeemed, redeemed_cap)
        th = result.get("transactionHash") or getattr(resp, "transaction_hash", None)
        log.info(
            "auto_redeem (relayer): redeemed conditionId=%s… tx=%s",
            cid[:18],
            th or "?",
        )


def _redeem_via_rpc(
    acct: Account,
    signer: str,
    polygon_rpc_url: str,
    condition_ids: list[str],
    redeemed: set[str],
    redeem_state_path: Path,
    redeemed_cap: int,
) -> None:
    w3 = Web3(Web3.HTTPProvider(polygon_rpc_url))
    if not w3.is_connected():
        log.warning("auto_redeem: Polygon RPC not connected — skip")
        return

    ctf = w3.eth.contract(address=CTF, abi=REDEEM_ABI)
    parent = bytes(32)

    for cid in condition_ids:
        try:
            cond_bytes = Web3.to_bytes(hexstr=cid)
        except Exception as e:
            log.warning("auto_redeem: bad conditionId %s…: %s", cid[:16], e)
            continue

        try:
            tx = ctf.functions.redeemPositions(
                USDC_E,
                parent,
                cond_bytes,
                INDEX_SETS_BINARY,
            ).build_transaction(
                {
                    "from": signer,
                    "chainId": CHAIN_ID,
                    "nonce": w3.eth.get_transaction_count(signer),
                }
            )
            gas = int(w3.eth.estimate_gas(tx) * 1.2)
            tx["gas"] = gas
            latest = w3.eth.get_block("latest")
            base = latest.get("baseFeePerGas")
            if base is not None:
                priority = w3.to_wei(30, "gwei")
                tx["maxPriorityFeePerGas"] = priority
                tx["maxFeePerGas"] = int(base * 2 + priority)
            else:
                tx["gasPrice"] = w3.eth.gas_price

            signed = acct.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            tx_hash = w3.eth.send_raw_transaction(raw)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
        except Exception as e:
            log.warning("auto_redeem: failed conditionId=%s…: %s", cid[:18], e)
            continue

        if receipt.get("status") != 1:
            log.warning(
                "auto_redeem: tx reverted conditionId=%s… tx=%s",
                cid[:18],
                receipt.get("transactionHash", b"").hex()
                if receipt.get("transactionHash")
                else "?",
            )
            continue

        redeemed.add(cid.lower())
        save_redeemed(redeem_state_path, redeemed, redeemed_cap)
        h = tx_hash.hex() if hasattr(tx_hash, "hex") else Web3.to_hex(tx_hash)
        log.info("auto_redeem (rpc): redeemed conditionId=%s… tx=%s", cid[:18], h)


def redeem_winnings_pass(
    *,
    private_key: str,
    funder: str | None,
    user_agent: str,
    dry_run: bool,
    polygon_rpc_url: str,
    redeem_state_path: Path,
    redeemed_cap: int,
    http: httpx.Client,
    poly_builder_key: str | None = None,
    poly_builder_secret: str | None = None,
    poly_builder_passphrase: str | None = None,
    relayer_url: str | None = None,
) -> None:
    acct = Account.from_key(private_key)
    signer = Web3.to_checksum_address(acct.address)
    resolved_funder = Web3.to_checksum_address(funder) if funder else signer

    use_relayer = _builder_creds_ok(
        poly_builder_key, poly_builder_secret, poly_builder_passphrase
    )
    relayer_u = (relayer_url or DEFAULT_RELAYER_URL).strip().rstrip("/")

    positions_wallet: str
    if use_relayer:
        w = _relayer_positions_wallet(private_key, funder)
        if w is None:
            return
        positions_wallet = w
    else:
        if resolved_funder.lower() != signer.lower():
            _log_proxy_skip_once()
            return
        positions_wallet = resolved_funder

    redeemed = load_redeemed(redeem_state_path, redeemed_cap)
    positions = fetch_redeemable_positions(http, positions_wallet, user_agent)
    condition_ids = [
        c for c in _condition_ids_from_positions(positions) if c.lower() not in redeemed
    ]

    if not condition_ids:
        log.debug("auto_redeem: no new redeemable conditions for %s", positions_wallet)
        return

    log.info(
        "auto_redeem: %d redeemable condition(s) for %s (mode=%s)",
        len(condition_ids),
        positions_wallet,
        "relayer" if use_relayer else "rpc",
    )

    if dry_run:
        for cid in condition_ids:
            log.info("[dry_run] would redeem conditionId=%s…", cid[:18])
        return

    if use_relayer:
        _redeem_via_relayer(
            private_key,
            relayer_u,
            poly_builder_key or "",
            poly_builder_secret or "",
            poly_builder_passphrase or "",
            condition_ids,
            redeemed,
            redeem_state_path,
            redeemed_cap,
        )
        return

    if not polygon_rpc_url:
        log.debug("auto_redeem: POLYGON_RPC_URL unset — cannot submit RPC redeem")
        return

    _redeem_via_rpc(
        acct,
        signer,
        polygon_rpc_url,
        condition_ids,
        redeemed,
        redeem_state_path,
        redeemed_cap,
    )
