#!/usr/bin/env bash
# Shared demo-tenant lookup (review finding: this snippet lived verbatim in
# both seed-demo.sh and e2e-compose-gate.sh, demo slug and all).
# Usage: lookup_tenant_id <console-base-url> <auth-header> [slug]
#
# E1.4: works for BOTH planes, because the two now carry different
# identities. A platform identity reads the admin listing; a TENANT
# identity is refused there (403 -- that endpoint is platform-plane, and
# correctly so) and reads its own membership instead. A tenant user
# should never need a platform-admin endpoint to learn its own tenant id.
lookup_tenant_id() {
  local console="$1" auth="$2" slug="${3:-tenant-demo}"
  local found
  found=$(curl -sf -H "$auth" "$console/api/admin/tenants/?search=$slug" 2>/dev/null \
    | python3 -c "
import sys, json
try:
    items = json.load(sys.stdin).get('items', [])
except Exception:
    items = []
match = [t for t in items if t.get('slug') == '$slug']
print(match[0]['id'] if match else '')
" 2>/dev/null || echo "")
  if [ -n "$found" ]; then
    echo "$found"
    return 0
  fi
  curl -sf -H "$auth" "$console/api/me/tenants" 2>/dev/null \
    | python3 -c "
import sys, json
try:
    rows = json.load(sys.stdin).get('tenants', [])
except Exception:
    rows = []
match = [t for t in rows if t.get('slug') == '$slug'] or rows
print(match[0]['id'] if match else '')
" 2>/dev/null || echo ""
}
