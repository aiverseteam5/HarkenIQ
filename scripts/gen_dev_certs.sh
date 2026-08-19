#!/usr/bin/env bash
# Generate a self-signed dev CA + server certificate for the Site Manager
# gRPC/TLS endpoint (SAN: localhost, site-manager, 127.0.0.1).
#
# Usage: scripts/gen_dev_certs.sh [output-dir]
# Default output: deploy/site_manager/certs/
set -euo pipefail

OUT_DIR="${1:-deploy/site_manager/certs}"
DAYS=825
mkdir -p "$OUT_DIR"

# CA
openssl genrsa -out "$OUT_DIR/ca.key" 4096 2>/dev/null
openssl req -x509 -new -nodes -key "$OUT_DIR/ca.key" -sha256 -days "$DAYS" \
    -subj "/CN=HarkenIQ Dev CA" -out "$OUT_DIR/ca.crt"

# Server key + CSR
openssl genrsa -out "$OUT_DIR/server.key" 2048 2>/dev/null
openssl req -new -key "$OUT_DIR/server.key" \
    -subj "/CN=site-manager" -out "$OUT_DIR/server.csr"

# Sign with SANs
cat > "$OUT_DIR/san.cnf" <<EOF
subjectAltName = DNS:localhost, DNS:site-manager, IP:127.0.0.1
EOF
openssl x509 -req -in "$OUT_DIR/server.csr" -CA "$OUT_DIR/ca.crt" \
    -CAkey "$OUT_DIR/ca.key" -CAcreateserial -days "$DAYS" -sha256 \
    -extfile "$OUT_DIR/san.cnf" -out "$OUT_DIR/server.crt"

rm -f "$OUT_DIR/server.csr" "$OUT_DIR/san.cnf" "$OUT_DIR/ca.srl"
chmod 600 "$OUT_DIR/ca.key" "$OUT_DIR/server.key"

echo "Wrote dev certificates to $OUT_DIR:"
echo "  ca.crt      — give to agents (site_manager.tls_ca)"
echo "  server.crt  — HARKEN_SM_TLS_CERT"
echo "  server.key  — HARKEN_SM_TLS_KEY"
