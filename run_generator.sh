#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
exec ./venv/bin/python 1_generate_proxy_configs.py "$@"
