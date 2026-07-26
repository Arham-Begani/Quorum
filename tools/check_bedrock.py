"""Is Bedrock actually usable? Answers in one command.

The console is not a reliable signal. It lists models that exist in a region,
which is not the same as models your account may invoke, and a brand-new
account can authenticate perfectly while refusing every runtime call. This
probes the thing that matters -- a real InvokeModel -- and tells you exactly
what to put in .env if it works.

    python tools/check_bedrock.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quorum.db.pool import load_env  # noqa: E402

EMBED_CANDIDATES = [
    "amazon.titan-embed-text-v2:0",
    "amazon.titan-embed-text-v1",
    "cohere.embed-english-v3",
    "cohere.embed-v4:0",
]
CHAT_PREFIXES = ("anthropic", "amazon.nova", "mistral", "qwen", "meta", "deepseek",
                 "google", "ai21", "writer")


def main() -> int:
    load_env()
    region = os.environ.get("AWS_REGION", "us-east-1")

    print("=" * 70)
    print("BEDROCK REACHABILITY CHECK")
    print("=" * 70)
    print(f"  region: {region}")

    have_iam = bool(os.environ.get("AWS_ACCESS_KEY_ID", "").strip())
    have_bearer = bool(os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "").strip())
    print(f"  IAM keys: {'yes' if have_iam else 'no'}    "
          f"Bedrock API key: {'yes' if have_bearer else 'no'}")
    if not (have_iam or have_bearer):
        print("\n  No credentials. Set AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY,")
        print("  or AWS_BEARER_TOKEN_BEDROCK, in .env")
        return 1
    if have_bearer and have_iam:
        print("\n  NOTE: both are set. botocore prefers the bearer token; if that key")
        print("  lacks bedrock:InvokeModel your IAM keys will never be tried.")

    try:
        import boto3
    except ImportError:
        print("\n  boto3 not installed: pip install boto3")
        return 1

    try:
        sts = boto3.client("sts", region_name=region)
        print(f"  identity: {sts.get_caller_identity()['Arn']}")
    except Exception as exc:
        print(f"  identity: could not resolve ({type(exc).__name__})")

    rt = boto3.client("bedrock-runtime", region_name=region)
    working_embed, working_chat = None, None

    # ---- embeddings ---------------------------------------------------
    print("\n--- embeddings ---")
    for mid in EMBED_CANDIDATES:
        body = ({"texts": ["probe"], "input_type": "search_document"}
                if mid.startswith("cohere") else {"inputText": "probe"})
        try:
            r = rt.invoke_model(modelId=mid, body=json.dumps(body),
                                accept="application/json", contentType="application/json")
            payload = json.loads(r["body"].read())
            vec = payload.get("embedding") or (payload.get("embeddings") or [[]])[0]
            print(f"  OK    {mid}  (dim {len(vec)})")
            working_embed = mid
            break
        except Exception as exc:
            print(f"  no    {mid}: {str(exc).split(':')[-1].strip()[:56]}")

    # ---- chat ----------------------------------------------------------
    print("\n--- chat (tier-2 adjudicator) ---")
    try:
        bd = boto3.client("bedrock", region_name=region)
        summaries = bd.list_foundation_models()["modelSummaries"]
        chat_ids = [m["modelId"] for m in summaries
                    if "TEXT" in (m.get("outputModalities") or [])
                    and m["modelId"].startswith(CHAT_PREFIXES)]
        print(f"  {len(summaries)} models visible, {len(chat_ids)} text-capable")
    except Exception as exc:
        chat_ids = []
        print(f"  could not list models: {type(exc).__name__}")

    for mid in chat_ids[:20]:
        for form in (f"us.{mid}", mid):     # newer models are inference-profile only
            try:
                rt.converse(modelId=form,
                            messages=[{"role": "user", "content": [{"text": "hi"}]}],
                            inferenceConfig={"maxTokens": 8, "temperature": 0})
                print(f"  OK    {form}")
                working_chat = form
                break
            except Exception:
                continue
        if working_chat:
            break
    if not working_chat:
        print(f"  no    none of {min(len(chat_ids), 20)} models responded")

    # ---- verdict -------------------------------------------------------
    print("\n" + "=" * 70)
    if working_embed or working_chat:
        print("BEDROCK IS USABLE — put these in .env:")
        if working_embed:
            print(f"  BEDROCK_EMBED_MODEL_ID={working_embed}")
        if working_chat:
            print(f"  BEDROCK_CHAT_MODEL_ID={working_chat}")
        print("\nThen re-run:  python -m quorum.harness.report --all --delay-ms 40")
        return 0

    print("BEDROCK IS NOT USABLE YET")
    print("  Credentials work but nothing invokes. That is account-level: model")
    print("  access is granted on first invocation now, and a new AWS account can")
    print("  take hours to finish provisioning. Check billing has a verified")
    print("  payment method, then try again later.")
    print("\n  Nothing is blocked meanwhile: embeddings run locally (a real")
    print("  semantic model) and tier-2 fails closed. See CONSISTENCY_MODEL.md §7.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
