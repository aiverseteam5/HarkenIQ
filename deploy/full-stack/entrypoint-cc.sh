#!/bin/sh
set -e
cd /app/cc
echo "Running alembic upgrade head..."
alembic upgrade head 2>/dev/null || echo "Alembic migration skipped (non-postgres or first run)"
echo "Starting Central Command..."
exec python -m harkeniq_cc
