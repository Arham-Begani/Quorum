#!/usr/bin/env bash
# Local 3-node insecure CockroachDB cluster for the chaos test.
#
# You cannot kill a node in CockroachDB Cloud Basic -- there are no nodes you
# can reach -- so the node-kill demo needs a local cluster. The consistency
# argument is identical; only the operator access differs.
#
#   bash infra/chaos/start_cluster.sh
#   export CRDB_URL='postgresql://root@localhost:26301/defaultdb?sslmode=disable'
#   python -m quorum.db.migrate
#   QUORUM_CHAOS=1 pytest tests/chaos -q -s
#   bash infra/chaos/stop_cluster.sh
set -euo pipefail

IMAGE="${CRDB_IMAGE:-cockroachdb/cockroach:latest}"
NET=quorum-chaos

# CockroachDB sizes its caches against total host memory, which on a laptop
# means three nodes will happily reserve more than the machine has. Left
# unconstrained this does not fail cleanly -- it exhausts the page file and the
# next process that tries to fork dies with an unrelated-looking error
# (WinError 1455 on Windows). Pin the caches and cap each container.
MEM="${CRDB_MEM:-900m}"
CACHE="${CRDB_CACHE:-128MiB}"
SQL_MEM="${CRDB_SQL_MEM:-128MiB}"

docker network create "$NET" 2>/dev/null || true

for i in 1 2 3; do
  port=$((26300 + i))
  http=$((8080 + i))
  docker rm -f "quorum-crdb-$i" 2>/dev/null || true
  docker run -d --name "quorum-crdb-$i" --hostname "quorum-crdb-$i" \
    --net "$NET" -m "$MEM" -p "${port}:26257" -p "${http}:8080" "$IMAGE" \
    start --insecure \
      --cache="$CACHE" --max-sql-memory="$SQL_MEM" \
      --join=quorum-crdb-1:26257,quorum-crdb-2:26257,quorum-crdb-3:26257 \
      --advertise-addr="quorum-crdb-$i:26257"
done

echo "waiting for nodes to come up..."
sleep 12
docker exec quorum-crdb-1 ./cockroach init --insecure 2>/dev/null || \
  echo "cluster already initialised"
sleep 6
# `node status` rather than crdb_internal.gossip_nodes: from v26.2 the internal
# tables are gated behind allow_unsafe_internals and the query errors out, which
# is not something you want on screen while demoing.
docker exec quorum-crdb-1 ./cockroach node status --insecure

cat <<'EOF'

Cluster is up on localhost:26301, 26302, 26303.

  export CRDB_URL='postgresql://root@localhost:26301/defaultdb?sslmode=disable'
  python -m quorum.db.migrate
  QUORUM_CHAOS=1 pytest tests/chaos -q -s

Notes
  - Needs roughly 3 GB free. Each node is capped at CRDB_MEM (default 900m)
    with a 128MiB cache; raise them with CRDB_MEM / CRDB_CACHE if you have
    headroom. Unconstrained, three nodes will exhaust a laptop's page file and
    the failure surfaces as an unrelated-looking spawn error.
  - The vector index needs a CockroachDB build that supports it. If
    CREATE VECTOR INDEX fails on this image, pin CRDB_IMAGE to v25.2+.
EOF
