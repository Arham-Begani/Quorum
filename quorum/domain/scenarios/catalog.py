"""The five canonical scenarios. (CLAUDE.md §8)

Each must reproduce its contradiction deterministically and produce a
different, legible failure in naive/txn_only.

  S1_checkin_date       tier 1  SUPERSEDE via R1   hotel booked for the wrong night
  S2_budget_ceiling     tier 2  REJECT    via R1   booking exceeds policy
  S3_ground_overlap     tier 1  CONTEST   via R4   double-booked transfer
  S4_preference_reversal tier 2 SUPERSEDE via R3   emails after opt-out
  S5_concurrent_race    tier 1  one commits, other 40001s, retries, CONTESTs

S5 is the flagship: the only scenario that isolates the isolation-level
argument, and the one that makes naive fail in a way txn_only does not.
"""

from __future__ import annotations

from ..inventory import Inventory
from .base import ActTurn, Expectation, RememberTurn, ScenarioPlan

INV = Inventory()

K_CHECKIN = "trip:1:hotel.checkin_date"
K_BUDGET = "trip:1:budget.ceiling_usd"
K_FLEX = "trip:1:traveller.price_flexibility"
K_TRANSFER = "trip:1:ground.transfer_slot"
K_CONTACT = "trip:1:traveller.contact_preference"

CORRECT_CHECKIN = "2026-09-15"     # the flight actually lands on the 15th
WRONG_CHECKIN = "2026-09-14"


# --------------------------------------------------------------------------
S1 = ScenarioPlan(
    id="S1_checkin_date",
    title="Check-in date contradiction",
    description=(
        "The lodging agent plans a check-in for Sep 14. The booking agent then "
        "reports the CONFIRMED itinerary, whose flight lands on Sep 15. Two "
        "structurally unrelated rows, both 'currently true', and only one of "
        "them can be."
    ),
    tier="tier1",
    turns=(
        RememberTurn("lodging-1", K_CHECKIN, "equals",
                     f"check-in is {WRONG_CHECKIN}", {"date": WRONG_CHECKIN},
                     confidence=0.7, label="lodging plans Sep 14"),
        RememberTurn("booking-1", K_CHECKIN, "equals",
                     f"confirmed check-in is {CORRECT_CHECKIN}", {"date": CORRECT_CHECKIN},
                     confidence=0.95, label="booking confirms Sep 15"),
        ActTurn("lodging-1", "book_hotel",
                {"hotel": INV.cheapest_hotel().name}, (K_CHECKIN,),
                label="book the hotel"),
    ),
    ground_truth={K_CHECKIN: {"date": CORRECT_CHECKIN}},
    expectations={
        "naive": Expectation(">0", ">0", "0",
                             "both dates appended; books the stale Sep 14 night"),
        "txn_only": Expectation(">0", ">0", "0",
                                "serializable and anomaly-free, and still books Sep 14"),
        "quorum": Expectation("0", "0", "0",
                              "R1: booking_agent (t1) supersedes lodging_agent (t3)"),
    },
    wrong_action_note="hotel booked for the wrong night; guest arrives to no room",
)

# --------------------------------------------------------------------------
S2 = ScenarioPlan(
    id="S2_budget_ceiling",
    title="Budget ceiling vs inferred flexibility",
    description=(
        "The budget agent records a hard ceiling of $2,400. The research agent "
        "infers from browsing history that the traveller is 'flexible on price'. "
        "Different subject keys, so tier 1 has no structural opinion -- this is "
        "the pair tier 2 exists for."
    ),
    tier="tier2",
    turns=(
        RememberTurn("budget-1", K_BUDGET, "equals",
                     "budget ceiling is 2400 USD", {"amount": 2400},
                     confidence=0.9, label="budget sets the ceiling"),
        RememberTurn("research-1", K_FLEX, "equals",
                     "traveller is flexible on price and will pay above 2400 USD",
                     {"flexible_above": 2400}, confidence=0.4,
                     label="research infers flexibility"),
        # Requires BOTH keys: the over-ceiling booking is only justifiable if
        # the 'flexible on price' inference is live memory. In quorum that
        # inference is REJECTED, so the justification does not exist and the
        # gate blocks.
        ActTurn("booking-1", "book_package",
                {"total_usd": 2900}, (K_BUDGET, K_FLEX),
                label="book a package over the ceiling"),
    ),
    ground_truth={K_BUDGET: {"amount": 2400}, K_FLEX: None},
    expectations={
        "naive": Expectation("0", ">0", "0",
                             "no cross-key reasoning at all; books over the ceiling"),
        "txn_only": Expectation("0", ">0", "0",
                                "isolation says nothing about a policy breach"),
        "quorum": Expectation("0", "0", ">0",
                              "R1: budget_agent (t2) outranks research_agent (t4); "
                              "the inference is rejected and the booking loses its "
                              "justification"),
    },
    wrong_action_note="booking exceeds policy; the expense is rejected later",
    constraints={"total_usd": {"key": K_BUDGET, "field": "amount"}},
    requires_semantic_embeddings=True,
)

# --------------------------------------------------------------------------
S3 = ScenarioPlan(
    id="S3_ground_overlap",
    title="Two ground agents, one transfer slot",
    description=(
        "Two ground agents each book an airport transfer for the same slot with "
        "identical authority, identical evidence and identical confidence. "
        "Nothing in the data says which is right -- and that is the point."
    ),
    tier="tier1",
    turns=(
        RememberTurn("ground-1", K_TRANSFER, "equals",
                     "airport transfer at 2026-09-15T09:00 with LisboaCars",
                     {"slot": "2026-09-15T09:00", "provider": "LisboaCars"},
                     confidence=0.7, label="ground-1 books LisboaCars"),
        RememberTurn("ground-2", K_TRANSFER, "equals",
                     "airport transfer at 2026-09-15T09:00 with TejoTransfers",
                     {"slot": "2026-09-15T09:00", "provider": "TejoTransfers"},
                     confidence=0.7, label="ground-2 books TejoTransfers"),
        ActTurn("ground-1", "book_transfer",
                {"slot": "2026-09-15T09:00"}, (K_TRANSFER,),
                label="confirm the transfer"),
    ),
    ground_truth={K_TRANSFER: None},   # genuinely undecidable from the data
    expectations={
        "naive": Expectation(">0", ">0", "0",
                             "two active transfers; books one, charges for both"),
        "txn_only": Expectation(">0", ">0", "0",
                                "both commit cleanly as unrelated rows"),
        "quorum": Expectation("0", "0", ">0",
                              "R4: same tier, same evidence -- CONTEST and block"),
    },
    wrong_action_note="double-booked and double-charged airport transfer",
)

# --------------------------------------------------------------------------
S4 = ScenarioPlan(
    id="S4_preference_reversal",
    title="Contact preference reversal",
    description=(
        "The traveller earlier preferred email updates; later they say stop "
        "emailing. Same writer role, same authority, no structural signal that "
        "one supersedes the other -- recency within a tier has to decide it."
    ),
    tier="tier2",
    turns=(
        # Predicate is `prefers`, not `equals`. Two different preferences can
        # coexist in principle (a person may prefer several things), so tier 1
        # deliberately abstains and the pair escalates to tier 2. Same subject
        # key, so ANN puts them adjacent and the escalation actually happens.
        RememberTurn("research-1", K_CONTACT, "prefers",
                     "traveller prefers email updates", {"channel": "email"},
                     confidence=0.6, label="earlier: prefers email"),
        RememberTurn("research-1", K_CONTACT, "prefers",
                     "traveller asked to stop receiving emails", {"channel": "none"},
                     confidence=0.8, label="later: opts out"),
        ActTurn("research-1", "send_update_email",
                {"template": "itinerary_update"}, (K_CONTACT,),
                label="send an itinerary email"),
    ),
    ground_truth={K_CONTACT: {"channel": "none"}},
    expectations={
        "naive": Expectation(">0", ">0", "0",
                             "both preferences active; keeps emailing after opt-out"),
        "txn_only": Expectation(">0", ">0", "0",
                                "two clean rows, one compliance failure"),
        "quorum": Expectation("0", "0", "0",
                              "R3: recency within a tier supersedes"),
    },
    wrong_action_note="agent keeps emailing after opt-out -- a compliance failure",
)

# --------------------------------------------------------------------------
S5 = ScenarioPlan(
    id="S5_concurrent_race",
    title="Simultaneous contradictory writes (flagship)",
    description=(
        "Two agents write contradictory check-in dates AT THE SAME INSTANT, "
        "released together by a barrier. This is the only scenario that "
        "isolates the isolation-level argument: in naive both writers read a "
        "neighbourhood that does not yet contain the other, both conclude 'no "
        "conflict', and both commit. Under serializable isolation one commits, "
        "the other takes a 40001, retries, sees the winner, and resolves."
    ),
    tier="tier1",
    turns=(
        RememberTurn("lodging-1", K_CHECKIN, "equals",
                     f"check-in is {WRONG_CHECKIN}", {"date": WRONG_CHECKIN},
                     confidence=0.7, concurrent_group="race",
                     label="writer A (simultaneous)"),
        RememberTurn("ground-1", K_CHECKIN, "equals",
                     f"check-in is {CORRECT_CHECKIN}", {"date": CORRECT_CHECKIN},
                     confidence=0.7, concurrent_group="race",
                     label="writer B (simultaneous)"),
        ActTurn("lodging-1", "book_hotel",
                {"hotel": INV.cheapest_hotel().name}, (K_CHECKIN,),
                label="book the hotel"),
    ),
    ground_truth={K_CHECKIN: None},    # undecidable: same tier, same confidence
    expectations={
        "naive": Expectation(">0", ">0", "0",
                             "both writers miss each other; memory holds two truths"),
        "txn_only": Expectation(">0", ">0", "0",
                                "no anomaly, no lost update -- and still two truths"),
        "quorum": Expectation("0", "0", ">0",
                              "one commits, the other 40001s and retries into CONTEST"),
    },
    wrong_action_note="books against a memory that holds two mutually exclusive dates",
)


CATALOG = {s.id: s for s in (S1, S2, S3, S4, S5)}
SCENARIO_IDS = tuple(CATALOG)


def get(scenario_id: str) -> ScenarioPlan:
    try:
        return CATALOG[scenario_id]
    except KeyError:
        raise ValueError(
            f"unknown scenario {scenario_id!r}; expected one of {', '.join(SCENARIO_IDS)}"
        ) from None
