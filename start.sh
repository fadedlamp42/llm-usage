#!/bin/sh
# start llm-usage daemon via PM2
cd "$(dirname "$0")"
poetry install --quiet 2>/dev/null
exec poetry run llm-usage
