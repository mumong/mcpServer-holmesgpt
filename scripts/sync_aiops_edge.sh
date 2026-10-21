#!/bin/sh
# Copy the shared multi-cluster client (robusta/aiops_edge, single source) into
# servers/aiops_edge. The runner subpackage stays in robusta. Checked by
# tests/test_aiops_edge_sync.py.
set -eu
src=${1:-../robusta/aiops_edge}
dst=servers/aiops_edge
mkdir -p "$dst"
for f in __init__.py signing.py kubectl_args.py clusters.py tunnel.py shim.py streams.py; do
  cp "$src/$f" "$dst/$f"
done
echo "synced $(ls $dst | wc -l) files from $src"
