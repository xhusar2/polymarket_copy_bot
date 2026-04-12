import argparse
import logging
import os

from dotenv import load_dotenv

from .bot import (
    clob_identity_check,
    redeem_winnings_once,
    replay_last_trades,
    run_loop,
    settings_from_env,
)


def main() -> None:
    load_dotenv()
    log_level = getattr(
        logging,
        os.environ.get("LOG_LEVEL", "INFO").upper(),
        logging.INFO,
    )
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(description="Polymarket copy trader (CLOB mirror)")
    p.add_argument(
        "--target",
        metavar="0x…",
        help="Leader wallet (proxy) to copy; overrides COPY_TARGET_WALLET",
    )
    p.add_argument(
        "--replay",
        nargs="?",
        const=100,
        type=int,
        metavar="N",
        help="One-shot: fetch last N leader trades and mirror each (default N=100). "
        "Updates state so duplicates are not re-fired in the next poll.",
    )
    p.add_argument(
        "--follow",
        action="store_true",
        help="After --replay, keep running the normal poll loop",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="Print signer/funder and CLOB USDC balance (no trades; COPY_TARGET_WALLET optional)",
    )
    p.add_argument(
        "--redeem-once",
        action="store_true",
        help="One-shot: redeem resolved positions on-chain (EOA only; COPY_TARGET_WALLET optional)",
    )
    args = p.parse_args()
    settings = settings_from_env(
        args.target,
        require_copy_target=not (args.check or args.redeem_once),
    )
    if args.check:
        raise SystemExit(clob_identity_check(settings))
    if args.redeem_once:
        redeem_winnings_once(settings)
        return
    if args.replay is not None:
        replay_last_trades(settings, args.replay)
        if not args.follow:
            return
    run_loop(settings)


if __name__ == "__main__":
    main()
