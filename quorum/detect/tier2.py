"""Tier 2 — bounded LLM adjudicator.

Only fires for pairs that tier 1 could not decide AND that sit above
TAU_ADJUDICATE cosine similarity. Temperature 0, strict JSON out, hard timeout,
hard call budget.

FAIL CLOSED. On timeout, parse failure, throttle, or missing credentials the
verdict is CONTRADICTION, which routes to CONTEST. A false contest is a
visible, safe outcome that a human resolves. A missed contradiction is the
exact failure this project exists to prevent. Never fail open.

Provider selection is explicit and always reported:

    bedrock_claude   real adjudication via Bedrock (needs AWS credentials)
    offline_stub     no model reachable -> every escalated pair fails closed

The offline stub does NOT classify. It returns CONTRADICTION for everything it
is asked, which is the specified failure behaviour, not a simulation of
judgement. Run reports record `tier2_provider` so a run without Bedrock is
never mistaken for one with it.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass

from ..db.metrics import metrics
from ..memory.schema import Atom, Claim, Verdict
from . import prompts

DEFAULT_TIMEOUT_S = 8.0
OFFLINE_STUB = "offline_stub"
BEDROCK = "bedrock_claude"

_JSON_RE = re.compile(r"\{.*\}", re.S)


@dataclass(frozen=True)
class Tier2Verdict:
    verdict: str
    confidence: float
    rationale: str
    latency_ms: float
    failed_closed: bool = False


class BudgetExhausted(RuntimeError):
    pass


class Adjudicator:
    """Bounded, fail-closed tier-2 classifier."""

    def __init__(
        self,
        *,
        model_id: str | None = None,
        region: str | None = None,
        per_claim_budget: int | None = None,
        run_ceiling: int | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        force_offline: bool = False,
    ):
        from ..db.pool import load_env      # see Embedder.__init__
        load_env()
        self.model_id = model_id or os.environ.get("BEDROCK_CHAT_MODEL_ID", "")
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self.per_claim_budget = int(
            per_claim_budget or os.environ.get("ADJUDICATE_BUDGET", 3))
        self.run_ceiling = int(run_ceiling or os.environ.get("ADJUDICATE_RUN_CEILING", 200))
        self.timeout_s = timeout_s
        self.calls_this_run = 0
        self._client = None
        self.selection_error: str | None = None
        self.provider = OFFLINE_STUB if force_offline else self._select_provider()

    def _select_provider(self) -> str:
        if not self.model_id:
            return OFFLINE_STUB
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            return OFFLINE_STUB
        try:
            from ..embed.bedrock import has_bedrock_auth
            session = boto3.session.Session(region_name=self.region)
            if not has_bedrock_auth(session):
                return OFFLINE_STUB
            self._client = session.client(
                "bedrock-runtime",
                config=Config(read_timeout=self.timeout_s, retries={"max_attempts": 2}),
            )
            # Prove the model actually answers. Credentials on an account with
            # no Bedrock entitlement authenticate fine and refuse every call --
            # which would fail closed on EVERY pair while the run report
            # claimed a real adjudicator was in use. That is precisely the
            # mislabelling the provider field exists to prevent.
            self._invoke('Reply with JSON only: {"verdict":"unrelated",'
                         '"confidence":1.0,"rationale":"probe"}')
            return BEDROCK
        except Exception as exc:
            self._client = None
            self.selection_error = f"{type(exc).__name__}: {str(exc)[:140]}"
            return OFFLINE_STUB

    @property
    def is_offline(self) -> bool:
        return self.provider == OFFLINE_STUB

    def reset_run(self) -> None:
        self.calls_this_run = 0

    # -- public ---------------------------------------------------------
    def adjudicate(self, incoming: Claim, existing: Atom, *, calls_used: int = 0) -> Tier2Verdict:
        """Classify one pair. Always returns a verdict; never raises for model errors."""
        if calls_used >= self.per_claim_budget:
            return self._closed("per-claim adjudication budget exhausted", 0.0)
        if self.calls_this_run >= self.run_ceiling:
            return self._closed("per-run adjudication ceiling reached", 0.0)

        self.calls_this_run += 1
        if self.is_offline:
            # Not a classification. The specified behaviour when no adjudicator
            # is reachable: fail closed so the pair becomes a visible CONTEST.
            return self._closed("no tier-2 adjudicator configured (offline stub)", 0.0)

        prompt = prompts.build(
            text_a=existing.object_text, role_a=existing.writer_role,
            text_b=incoming.object_text, role_b=incoming.role,
        )
        t0 = time.perf_counter()
        try:
            raw, tok_in, tok_out = self._invoke(prompt)
            ms = (time.perf_counter() - t0) * 1000.0
            parsed = self._parse(raw)
            metrics.count_adjudication(ms, tok_in, tok_out, failed=parsed is None)
            if parsed is None:
                return self._closed("adjudicator returned unparseable output", ms)
            verdict, confidence, rationale = parsed
            return Tier2Verdict(verdict, confidence, rationale, ms)
        except Exception as exc:  # timeout, throttle, anything
            ms = (time.perf_counter() - t0) * 1000.0
            metrics.count_adjudication(ms, 0, 0, failed=True)
            return self._closed(f"adjudicator error: {type(exc).__name__}", ms)

    # -- internals ------------------------------------------------------
    def _closed(self, why: str, ms: float) -> Tier2Verdict:
        return Tier2Verdict(Verdict.CONTRADICTION, 0.0, f"fail-closed: {why}", ms,
                            failed_closed=True)

    def _invoke(self, prompt: str) -> tuple[str, int, int]:
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 200,
            "temperature": 0,
            "system": prompts.ADJUDICATOR_SYSTEM,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        })
        resp = self._client.invoke_model(  # type: ignore[union-attr]
            modelId=self.model_id, body=body,
            accept="application/json", contentType="application/json",
        )
        payload = json.loads(resp["body"].read())
        text = "".join(part.get("text", "") for part in payload.get("content", []))
        usage = payload.get("usage", {})
        return text, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))

    @staticmethod
    def _parse(raw: str) -> tuple[str, float, str] | None:
        m = _JSON_RE.search(raw or "")
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        verdict = str(obj.get("verdict", "")).strip().lower()
        if verdict not in Verdict.ALL:
            return None
        try:
            confidence = float(obj.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        rationale = str(obj.get("rationale", ""))[:200]
        return verdict, max(0.0, min(1.0, confidence)), rationale

    def info(self) -> dict:
        return {
            "provider": self.provider,
            "unavailable": self.selection_error,
            "model_id": self.model_id or None,
            "prompt_version": prompts.PROMPT_VERSION,
            "per_claim_budget": self.per_claim_budget,
            "run_ceiling": self.run_ceiling,
            "calls_this_run": self.calls_this_run,
        }
