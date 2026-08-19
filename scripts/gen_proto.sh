#!/usr/bin/env bash
# Regenerate gRPC Python bindings from src/harkeniq/proto/harkeniq.proto.
# The generated *_pb2*.py files are checked in; rerun after editing the proto.
set -euo pipefail
cd "$(dirname "$0")/.."

python -m grpc_tools.protoc \
    -I src/harkeniq/proto \
    --python_out=src/harkeniq/proto \
    --grpc_python_out=src/harkeniq/proto \
    src/harkeniq/proto/harkeniq.proto

# grpc_tools emits an absolute sibling import; rewrite to package-relative.
sed -i 's/^import harkeniq_pb2 as harkeniq__pb2$/from harkeniq.proto import harkeniq_pb2 as harkeniq__pb2/' \
    src/harkeniq/proto/harkeniq_pb2_grpc.py

echo "Generated src/harkeniq/proto/harkeniq_pb2.py and harkeniq_pb2_grpc.py"
