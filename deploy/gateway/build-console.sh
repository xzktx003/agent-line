#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../../upstream/qwenpaw/console"
VITE_CONSOLE_BASENAME=/qwenpaw VITE_API_BASE_URL=/qwenpaw TOKEN= node node_modules/vite/bin/vite.js build --outDir ../../../deploy/gateway/qwen-console --emptyOutDir
