#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
exec .venv/bin/python -m uvicorn gateway:app --app-dir deploy/gateway --host 0.0.0.0 --port 3194 --no-access-log --no-proxy-headers
