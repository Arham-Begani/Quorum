"""Adjudicator prompt. VERSIONED — do not edit casually.

Changing this prompt changes tier-2 verdicts, which changes scenario outcomes,
which invalidates every recorded run report. If you must change it, bump
PROMPT_VERSION so old reports remain interpretable.
"""

PROMPT_VERSION = "v1"

ADJUDICATOR_SYSTEM = (
    "You are a strict consistency checker for an AI agent's memory. "
    "You answer with JSON only, never prose."
)

ADJUDICATOR_TEMPLATE = """You are a strict consistency checker for an AI agent's memory. You are given two
claims about the same trip. Decide their logical relationship.

CLAIM A (existing, written by role={role_a}): {text_a}
CLAIM B (incoming, written by role={role_b}): {text_b}

Answer with JSON only, no prose:
{{"verdict": "agreement"|"refinement"|"contradiction"|"unrelated",
 "confidence": 0.0-1.0,
 "rationale": "<= 20 words"}}

Definitions:
- agreement: both can be true and they assert the same thing
- refinement: both can be true; one is strictly more specific
- contradiction: they CANNOT both be true at the same time
- unrelated: they concern different facts

If uncertain, answer "contradiction". A false alarm is safe; a missed
contradiction is not."""


def build(text_a: str, role_a: str, text_b: str, role_b: str) -> str:
    return ADJUDICATOR_TEMPLATE.format(
        role_a=role_a, text_a=text_a, role_b=role_b, text_b=text_b
    )
