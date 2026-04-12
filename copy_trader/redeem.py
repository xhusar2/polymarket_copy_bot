"""
On-chain CTF redeemPositions for resolved Polymarket markets.

Only works when conditional tokens sit on the same address as PRIVATE_KEY (EOA /
POLYMARKET_SIGNATURE_TYPE=0 with no separate proxy funder). If POLYMARKET_FUNDER
differs from the signer, tokens live on the proxy — this module cannot submit
those txs without Safe / Polymarket relayer integration.
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
USDC_E = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
CTF = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
# Binary markets: redeem both index sets in one call (Polymarket CTF docs).
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


def _log_proxy_skip_once() -> None:
    global _logged_proxy_skip
    if _logged_proxy_skip:
        return
    _logged_proxy_skip = True
    log.warning(
        "AUTO_REDEEM: funder != signer — outcome tokens are on your Polymarket proxy. "
        "On-chain redeem must be sent from that address (Safe / UI). "
        "AUTO_REDEEM only works for EOA accounts (type 0, unset POLYMARKET_FUNDER)."
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
) -> None:
    acct = Account.from_key(private_key)
    signer = Web3.to_checksum_address(acct.address)
    resolved_funder = Web3.to_checksum_address(funder) if funder else signer

    if resolved_funder.lower() != signer.lower():
        _log_proxy_skip_once()
        return

    redeemed = load_redeemed(redeem_state_path, redeemed_cap)
    positions = fetch_redeemable_positions(http, resolved_funder, user_agent)
    condition_ids = [c for c in _condition_ids_from_positions(positions) if c.lower() not in redeemed]

    if not condition_ids:
        log.debug("auto_redeem: no new redeemable conditions for %s", resolved_funder)
        return

    log.info(
        "auto_redeem: %d redeemable condition(s) for %s (not yet recorded as redeemed)",
        len(condition_ids),
        resolved_funder,
    )

    if dry_run:
        for cid in condition_ids:
            log.info("[dry_run] would redeem conditionId=%s…", cid[:18])
        return

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
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
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
        log.info("auto_redeem: redeemed conditionId=%s… tx=%s", cid[:18], h)
