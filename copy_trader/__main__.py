import argparse
import logging

from .bot import run_loop, settings_from_env


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    p = argparse.ArgumentParser(description="Polymarket copy trader (CLOB mirror)")
    p.add_argument(
        "--target",
        metavar="0x…",
        help="Leader wallet (proxy) to copy; overrides COPY_TARGET_WALLET",
    )
    args = p.parse_args()
    run_loop(settings_from_env(args.target))


if __name__ == "__main__":
    main()
