#!/usr/bin/env bash
# Build the HarkenIQ agent container image locally (R6-P8).
# CI publishes multi-arch to GHCR on version tags (publish-agent.yml);
# this script is the local/dev equivalent. Tag = agent version from
# pyproject.toml unless overridden: ./scripts/build-agent-image.sh [tag]
set -euo pipefail
cd "$(dirname "$0")/.."

TAG="${1:-$(grep -m1 '^version' pyproject.toml | sed 's/.*"\(.*\)"/\1/')}"
docker build -f deploy/full-stack/Dockerfile.agent -t "harkeniq-agent:${TAG}" .
echo "Built harkeniq-agent:${TAG}"
