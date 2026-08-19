#!/bin/sh
# Apply schema migrations, then start the Site Manager.
set -e

cd /app/sm
echo "Running alembic upgrade head..."
alembic upgrade head
echo "Starting Site Manager..."
exec python -m harkeniq_sm
