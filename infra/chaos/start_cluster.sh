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

docker network create "$NET" 2>/dev/null || true

for i in 1 2 3; do
  port=$((26300 + i))
  http=$((8080 + i))
  docker rm -f "quorum-crdb-$i" 2>/dev/null || true
  docker run -d --name "quorum-crdb-$i" --hostname "quorum-crdb-$i" \
    --net "$NET" -p "${port}:26257" -p "${http}:8080" "$IMAGE" \
    start --insecure \
      --join=quorum-crdb-1:26257,quorum-crdb-2:26257,quorum-crdb-3:26257 \
      --advertise-addr="quorum-crdb-$i:26257"
done

echo "waiting for nodes to come up..."
sleep 12
docker exec quorum-crdb-1 ./cockroach init --insecure 2>/dev/null || \
  echo "cluster already initialised"
sleep 6
docker exec quorum-crdb-1 ./cockroach sql --insecure \
  -e "SELECT node_id, is_available, is_live FROM crdb_internal.gossip_nodes ORDER BY node_id;"

cat <<'EOF'

Cluster is up on localhost:26301, 26302, 26303.

  export CRDB_URL='postgresql://root@localhost:26301/defaultdb?sslmode=disable'
  python -m quorum.db.migrate
  QUORUM_CHAOS=1 pytest tests/chaos -q -s

Note: the vector index requires a CockroachDB build that supports it. If
CREATE VECTOR INDEX fails on this image, pin CRDB_IMAGE to a version that has
it (v25.2+).
EOF
