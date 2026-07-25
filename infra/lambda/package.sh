#!/usr/bin/env bash
# Package the agent-turn Lambda.
#
# NOT RUN in this submission -- no AWS credentials were configured. Written and
# documented as unrun in docs/SUBMISSION.md.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
BUILD="$ROOT/.build/lambda"
ZIP="$ROOT/.build/quorum-lambda.zip"

rm -rf "$BUILD" && mkdir -p "$BUILD"

# The memory core only. No fastapi, no uvicorn, no pytest -- a Lambda bundle
# that carries the test suite is a Lambda bundle that cold-starts slowly.
python -m pip install --quiet --target "$BUILD" \
  "psycopg[binary,pool]>=3.1" boto3 certifi python-dotenv

cp -r "$ROOT/quorum" "$BUILD/quorum"
cp "$HERE/handler.py" "$BUILD/handler.py"

find "$BUILD" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$BUILD" -name "*.dist-info" -type d -exec rm -rf {} + 2>/dev/null || true

( cd "$BUILD" && zip -qr "$ZIP" . )
echo "built $ZIP ($(du -h "$ZIP" | cut -f1))"

cat <<EOF

Upload and deploy:

  aws s3 cp "$ZIP" s3://quorum-deploy-<suffix>/quorum-lambda.zip
  aws cloudformation deploy \\
    --template-file infra/cloudformation/swarm.yaml \\
    --stack-name quorum \\
    --capabilities CAPABILITY_IAM \\
    --parameter-overrides BucketSuffix=<suffix> CrdbSecretArn=<arn>
EOF
