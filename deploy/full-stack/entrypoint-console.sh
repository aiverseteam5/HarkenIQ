#!/bin/sh
set -e
cd /app/console
echo "Running alembic upgrade head..."
alembic upgrade head 2>/dev/null || echo "Alembic migration skipped (non-postgres or first run)"
echo "Starting Console..."
exec python -m harkeniq_console
