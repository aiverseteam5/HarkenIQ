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

# R6: gNMI bindings (vendored from github.com/openconfig/gnmi; the
# gnmi_ext import inside gnmi.proto is rewritten to "gnmi_ext/gnmi_ext.proto"
# at vendor time so -I resolves it locally).
python -m grpc_tools.protoc \
    -I src/harkeniq/proto \
    --python_out=src/harkeniq/proto \
    --grpc_python_out=src/harkeniq/proto \
    src/harkeniq/proto/gnmi/gnmi.proto \
    src/harkeniq/proto/gnmi_ext/gnmi_ext.proto

sed -i 's/^from gnmi_ext import gnmi_ext_pb2 as gnmi__ext_dot_gnmi__ext__pb2$/from harkeniq.proto.gnmi_ext import gnmi_ext_pb2 as gnmi__ext_dot_gnmi__ext__pb2/' \
    src/harkeniq/proto/gnmi/gnmi_pb2.py
sed -i 's/^from gnmi import gnmi_pb2 as gnmi_dot_gnmi__pb2$/from harkeniq.proto.gnmi import gnmi_pb2 as gnmi_dot_gnmi__pb2/' \
    src/harkeniq/proto/gnmi/gnmi_pb2_grpc.py

echo "Generated harkeniq, gnmi, and gnmi_ext bindings under src/harkeniq/proto/"
