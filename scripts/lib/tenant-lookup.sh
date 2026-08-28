#!/usr/bin/env bash
# Shared demo-tenant lookup (review finding: this snippet lived verbatim in
# both seed-demo.sh and e2e-compose-gate.sh, demo slug and all).
# Usage: lookup_tenant_id <console-base-url> <auth-header> [slug]
lookup_tenant_id() {
  local console="$1" auth="$2" slug="${3:-tenant-demo}"
  curl -sf -H "$auth" "$console/api/admin/tenants/?search=$slug" \
    | python3 -c "
import sys, json
items = json.load(sys.stdin).get('items', [])
match = [t for t in items if t.get('slug') == '$slug']
print(match[0]['id'] if match else '')
"
}
