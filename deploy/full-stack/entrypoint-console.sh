#!/bin/sh
# Console entrypoint: schema first, then serve (QA-001).
# Migrations fail LOUDLY — see entrypoint-cc.sh.
set -e
cd /app/services/console
echo "console: alembic upgrade head"
alembic upgrade head
echo "console: starting"
exec python -m harkeniq_console
