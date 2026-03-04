#!/usr/bin/env bash
set -euo pipefail

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  source .env
fi

exec uvicorn backend.app:app --host 0.0.0.0 --port "${PORT:-8000}"
