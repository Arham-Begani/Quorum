"""Export run artifacts to AWS: the report to S3, the counters to CloudWatch.

Two rules govern this module, both inherited from the rest of the project:

1. **Nothing silently degrades.** Every export returns a structured status that
   is written into the run report, so a report can never imply an export
   happened when it did not. An `AccessDenied` is recorded as a denial, not
   swallowed into a shrug.
2. **An export failure never fails the run.** The consistency result is the
   science; shipping the telemetry is not. A denied metric push must not turn a
   green three-mode comparison red.

The metric set is the one BUILD.md §9.2 asks for — `txn_retries`,
`contradictions_detected`, `blocked_actions` — dimensioned by mode and
scenario, which is what makes the CloudWatch graph legible: three lines, and
only one of them is ever allowed to block anything.

Required IAM is deliberately tiny; see `infra/aws/quorum-least-privilege.json`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

NAMESPACE = "Quorum"

# The counters worth graphing, and where they live in a per-mode report.
METRICS: tuple[tuple[str, str, str], ...] = (
    ("TxnRetries", "performance", "txn_retries"),
    ("ContradictoryActivePairs", "anomalies", "contradictory_active_pairs"),
    ("WrongActions", "anomalies", "wrong_actions"),
    ("BlockedActions", "anomalies", "blocked_actions"),
    ("ContestedAtoms", "anomalies", "contested_atoms"),
)


def _status(attempted: bool, ok: bool, detail: str) -> dict[str, Any]:
    return {"attempted": attempted, "ok": ok, "detail": detail}


def _session():
    """A boto3 session, or None when boto3/credentials are absent."""
    try:
        import boto3
    except ImportError:
        return None
    session = boto3.session.Session(
        region_name=os.environ.get("AWS_REGION") or None
    )
    return session if session.get_credentials() is not None else None


DENIED_CODES = ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
                "InvalidAccessKeyId", "SignatureDoesNotMatch")


def _aws_error(exc: Exception) -> str:
    """Turn a botocore error into something a human can act on.

    boto3's `upload_file` raises S3UploadFailedError, which is NOT a ClientError
    and carries no `.response` — the underlying code only survives inside the
    message string. Without scanning the text, the single most likely real
    failure (an S3 AccessDenied before the policy is attached) would lose its
    "attach the policy" hint, which is the one thing the reader needs.
    """
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    text = str(exc)
    if not code:
        code = next((c for c in DENIED_CODES + ("NoSuchBucket",) if c in text), "")

    if code in DENIED_CODES:
        return (f"{code} — the IAM principal lacks this permission. Attach "
                f"infra/aws/quorum-least-privilege.json to it.")
    if code == "NoSuchBucket":
        return (f"NoSuchBucket — S3_BUCKET points at a bucket that does not "
                f"exist. Create it, or clear S3_BUCKET to skip the upload.")
    if code:
        return f"{code}: {text[:160]}"
    return f"{type(exc).__name__}: {text[:160]}"


def upload_report(path: Path) -> dict[str, Any]:
    """Upload the run report JSON to S3 under one prefix."""
    bucket = (os.environ.get("S3_BUCKET") or "").strip()
    if not bucket:
        return _status(False, False, "S3_BUCKET not set")

    session = _session()
    if session is None:
        return _status(False, False, "no AWS credentials or boto3 unavailable")

    key = f"runs/{path.name}"
    try:
        session.client("s3").upload_file(str(path), bucket, key)
        return _status(True, True, f"s3://{bucket}/{key}")
    except Exception as exc:
        return _status(True, False, _aws_error(exc))


def _datums(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for result in results:
        scenario = result.get("scenario", "unknown")
        for mode, report in (result.get("modes") or {}).items():
            if not isinstance(report, dict):
                continue
            dims = [{"Name": "Mode", "Value": str(mode)},
                    {"Name": "Scenario", "Value": str(scenario)}]
            for metric_name, section, field in METRICS:
                value = (report.get(section) or {}).get(field)
                if value is None:
                    continue
                out.append({
                    "MetricName": metric_name,
                    "Dimensions": dims,
                    "Value": float(value),
                    "Unit": "Count",
                })
    return out


def export_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Push the run counters to CloudWatch as custom metrics."""
    if (os.environ.get("CLOUDWATCH_METRICS") or "").strip().lower() in ("0", "off", "false"):
        return _status(False, False, "disabled by CLOUDWATCH_METRICS")

    session = _session()
    if session is None:
        return _status(False, False, "no AWS credentials or boto3 unavailable")

    datums = _datums(results)
    if not datums:
        return _status(False, False, "no metric data in this run")

    client = session.client("cloudwatch")
    try:
        # PutMetricData caps at 1000 datums per call; batch well under it.
        for i in range(0, len(datums), 20):
            client.put_metric_data(Namespace=NAMESPACE, MetricData=datums[i:i + 20])
        return _status(True, True, f"{len(datums)} datums -> CloudWatch/{NAMESPACE}")
    except Exception as exc:
        return _status(True, False, _aws_error(exc))


def print_status(label: str, status: dict[str, Any]) -> None:
    """One line, and never a lie about what happened."""
    if not status["attempted"]:
        print(f"  {label}: not attempted ({status['detail']})")
    elif status["ok"]:
        print(f"  {label}: {status['detail']}")
    else:
        print(f"  {label}: FAILED — {status['detail']}")
