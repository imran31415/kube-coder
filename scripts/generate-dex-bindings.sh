#!/usr/bin/env bash
set -euo pipefail
# Isolated build environment: grpcio-tools==1.78.0. Schema: Dex v2.44.0,
# api/v2/api.proto (Apache-2.0). Generated files are committed; no runtime protoc.
python -m grpc_tools.protoc -I charts/workspace-controller \
  --python_out=charts/workspace-controller \
  --grpc_python_out=charts/workspace-controller \
  charts/workspace-controller/dex_api.proto
