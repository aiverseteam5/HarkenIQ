#!/bin/sh
# Central Command entrypoint: schema first, then serve (QA-001).
# Migrations fail LOUDLY — a swallowed migration error is how CC shipped
# with no schema on Postgres. Do not add "|| echo skipped" here.
set -e
cd /app/services/central_command
echo "central-command: alembic upgrade head"
alembic upgrade head
echo "central-command: starting"
exec python -m harkeniq_cc
