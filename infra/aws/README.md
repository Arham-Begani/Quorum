# AWS — run reports to S3, counters to CloudWatch

Two exports, both least-privilege, both honest about whether they actually ran.

## Why this exists separately from Bedrock

Bedrock model invocation is gated at the account level and currently returns
`Operation not allowed` for every model (`make check-bedrock` shows the detail).
S3 and CloudWatch are **not** blocked by that — they need nothing more than an
IAM policy on the calling principal. So the AWS integration does not have to
wait on Bedrock provisioning.

## Setup

1. **Create a bucket** (any name; it goes in `S3_BUCKET`):

   ```bash
   aws s3 mb s3://quorum-runs-<suffix> --region us-east-1
   ```

2. **Attach the policy.** Edit `quorum-least-privilege.json` and replace
   `REPLACE-WITH-YOUR-BUCKET` with the bucket name, then attach it to the IAM
   user or role whose credentials are in `.env`:

   ```bash
   aws iam put-user-policy \
     --user-name Arham \
     --policy-name QuorumRunExport \
     --policy-document file://infra/aws/quorum-least-privilege.json
   ```

   What it grants, and nothing else:

   | permission | scope |
   |---|---|
   | `s3:PutObject` / `s3:GetObject` | the `runs/` prefix of one bucket |
   | `s3:ListBucket` | that bucket, `runs/*` prefix only |
   | `cloudwatch:PutMetricData` | **conditioned on** `namespace = Quorum` |
   | `cloudwatch:Get*` / `ListMetrics` | read back, for the dashboard |

   The `PutMetricData` condition is the part worth noting: the credential can
   publish into the `Quorum` namespace and no other. A leaked key cannot
   pollute another team's metrics.

3. **Point `.env` at the bucket:**

   ```bash
   S3_BUCKET=quorum-runs-<suffix>
   ```

4. **Run anything that produces a report:**

   ```bash
   make demo-s5
   ```

   The tail of the output tells you exactly what happened:

   ```
   AWS export
     S3        : s3://quorum-runs-xyz/runs/S5_concurrent_race.json
     CloudWatch: 15 datums -> CloudWatch/Quorum
   ```

   or, before the policy is attached:

   ```
     S3        : FAILED — AccessDenied — the IAM principal lacks this
                 permission. Attach infra/aws/quorum-least-privilege.json to it.
   ```

## Metrics published

Namespace `Quorum`, dimensioned by `Mode` and `Scenario`:

| metric | why it is on the graph |
|---|---|
| `TxnRetries` | 40001s are the database refusing to break serializability — bounded and visible, never hidden |
| `ContradictoryActivePairs` | the headline anomaly. Non-zero in `naive` and `txn_only`, zero in `quorum` |
| `WrongActions` | the user-visible consequence |
| `BlockedActions` | the gate working. High in `quorum`, zero elsewhere |
| `ContestedAtoms` | how often the policy engine declined to guess |

Graphing `ContradictoryActivePairs` by `Mode` gives you the whole thesis as one
CloudWatch chart: two lines above zero, one on it.

## Nothing silently degrades

Every export writes its own outcome into the run report under `aws`:

```json
"aws": {
  "s3":         {"attempted": true, "ok": true,  "detail": "s3://…/runs/S5.json"},
  "cloudwatch": {"attempted": true, "ok": false, "detail": "AccessDenied — …"}
}
```

A report can therefore never imply an export happened when it did not — the
same rule the embedder and tier-2 adjudicator already follow. An export failure
is logged and recorded but never fails the run: the consistency result is the
science, shipping the telemetry is not.
