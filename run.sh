#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "Creating .venv …" >&2
  if ! python3 -m venv .venv 2>/dev/null; then
    echo "venv failed. On Ubuntu/Debian install: sudo apt install python3.12-venv" >&2
    echo "Then run this script again." >&2
    exit 1
  fi
fi

"$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt"
exec "$ROOT/.venv/bin/python" -m copy_trader "$@"
