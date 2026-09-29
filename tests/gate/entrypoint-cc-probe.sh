#!/bin/sh
# TEST-ONLY (S3-E1, spec A30.37 answer 2 as amended by A30.38). Never shipped.
#
# The compose gate's own override mounts this directory read-only at
# /opt/harken-test and makes this Central Command's entrypoint. It runs the
# SHIPPED entrypoint with its final line -- and ONLY that line -- replaced by
# the global safety test harness, so the schema step (`alembic upgrade head`)
# is the shipped one byte for byte, including when a gate step stops and
# restarts Central Command.
#
#   entrypoint-cc-probe.sh <trigger-file> [shipped-entrypoint] [--print]
#
# --print writes the derived script to stdout instead of running it (the unit
# test's view of it).
set -eu
TRIGGER=${1:?usage: entrypoint-cc-probe.sh <trigger-file> [shipped-entrypoint] [--print]}
SHIPPED=${2:-/entrypoint.sh}
MODE=${3:-run}
HARNESS=/opt/harken-test/cc_global_safety_probe.py
LAST='exec python -m harkeniq_cc'

# Refuse rather than guess: if the shipped entrypoint no longer ends in the
# one line this replaces, the derivation would no longer be "the shipped
# entrypoint, with its final line replaced".
if [ "$(tail -n 1 "$SHIPPED")" != "$LAST" ] || [ "$(grep -cx "$LAST" "$SHIPPED")" != "1" ]; then
  echo "entrypoint-cc-probe: $SHIPPED does not end in exactly one '$LAST'" >&2
  exit 1
fi

derive() {
  sed '$d' "$SHIPPED"
  printf 'exec python %s %s\n' "$HARNESS" "$TRIGGER"
}

if [ "$MODE" = "--print" ]; then
  derive
  exit 0
fi
DERIVED=/tmp/entrypoint-cc-probe.derived.sh
derive > "$DERIVED"
exec sh "$DERIVED"
