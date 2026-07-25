#!/usr/bin/env bash
# Tear down the local chaos cluster.
set -euo pipefail
for i in 1 2 3; do
  docker rm -f "quorum-crdb-$i" 2>/dev/null || true
done
docker network rm quorum-chaos 2>/dev/null || true
echo "chaos cluster removed"
