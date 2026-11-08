#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -d web/node_modules ]; then
    npm --prefix web ci
fi
npm --prefix web run build
exec uv run --frozen m100-bot
