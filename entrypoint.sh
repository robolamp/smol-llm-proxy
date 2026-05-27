#!/bin/sh
# Ensure data directory exists and is writable
mkdir -p "$(dirname ${DB_PATH:-data/proxy.db})" 2>/dev/null || true
exec "$@"
