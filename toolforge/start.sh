#!/usr/bin/env bash
set -euo pipefail

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  source .env
fi

if [[ "${SELF_TEST_ON_BOOTUP:-0}" == "1" ]]; then
  echo "Running self-tests before startup..."
  pytest -q
fi

exec uvicorn backend.app:app --host 0.0.0.0 --port "${PORT:-8000}"
