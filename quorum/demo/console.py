"""Live console — type your own claims and watch the memory layer react.

The scripted scenarios are a controlled experiment: the input is fixed so the
memory layer is the only variable. That is the right way to MEASURE the effect,
but from the outside a scripted contradiction being caught by scripted code is
indistinguishable from a hardcoded demo.

This is the answer to that. Nobody here has seen your input before. The same
tier-1 classifier, the same policy engine and the same action gate that run in
the scenarios run on whatever you type.

The `mode` command is the point: enter the same two facts under `naive` and
under `quorum` and watch the same input produce different outcomes. If any of
this were hardcoded, it could not do that.

    python -m quorum.demo.console

    > remember lodging_agent hotel.checkin_date = 2026-09-14
    > remember booking_agent hotel.checkin_date = 2026-09-15
    > act book_hotel needs hotel.checkin_date
    > mode naive
    > reset
"""

from __future__ import annotations

import re
import sys
import uuid

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ..db.metrics import metrics
from ..db.pool import crdb_url, explain_connect_failure, make_pool, quorum_dbname
from ..detect.coerce import coerce_date, coerce_number
from ..detect.tier2 import Adjudicator
from ..embed.bedrock import Embedder
from ..memory.factory import MODES, make_memory
from ..memory.keys import normalize
from ..memory.schema import Action, AgentCtx, Claim
from ..policy.tiers import ROLE_TIERS

BANNER = r"""
  ___                            _ _
 / _ \ _   _  ___  _ __ _   _ _ | (_)_   _____
| | | | | | |/ _ \| '__| | | | '_ \ | \ \ / / _ \   live console
| |_| | |_| | (_) | |  | |_| | | | || |\ V /  __/   type anything -- nothing here is scripted
 \__\_\\__,_|\___/|_|   \__,_|_| |_||_| \_/ \___|
"""

HELP = """
  remember <role> <attribute> = <value>     write a claim
  remember <role> <attribute> prefers <v>   write with a different predicate
  act <action> needs <attribute>[,<attr>]   try to do something with memory
  show                                      current memory state
  conflicts                                 what has been detected so far
  mode [naive|txn_only|quorum]              swap the memory layer, keep the data
  roles                                     who can write, and their authority
  reset                                     wipe this workspace
  help / quit

  attribute is shorthand: "hotel.checkin_date" becomes trip:1:hotel.checkin_date
  values are coerced -- "Sep 14 2026" and "2026-09-14" are the same value
"""

C = {"g": "\033[32m", "r": "\033[31m", "y": "\033[33m", "b": "\033[34m",
     "d": "\033[90m", "B": "\033[1m", "x": "\033[0m"}


def col(s, c):
    return f"{C[c]}{s}{C['x']}"


RESOLUTION_COLOUR = {"accept": "g", "reinforce": "g", "supersede": "y",
                     "reject": "y", "contest": "r", "error": "r"}


def parse_value(raw: str):
    raw = raw.strip()
    if not raw:
        return None
    d = coerce_date(raw)
    if d is not None:
        return {"date": d.isoformat()}
    n = coerce_number(raw)
    if n is not None:
        return {"amount": n}
    return {"value": raw.strip('"\'')}


class Console:
    def __init__(self):
        self.pool = make_pool(crdb_url(), min_size=2, max_size=6,
                              dbname=quorum_dbname(), app_name="quorum-console")
        self.embedder = Embedder()
        self.adjudicator = Adjudicator()
        self.workspace = uuid.uuid4()
        self.mode = "quorum"
        self.run_id = uuid.uuid4()
        self._register_run()
        self.memory = self._build()

    def _register_run(self):
        with self.pool.connection() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("INSERT INTO run (run_id, mode, scenario, seed, workspace_id) "
                            "VALUES (%s,%s,'live_console',0,%s)",
                            (self.run_id, self.mode, self.workspace))

    def _build(self):
        return make_memory(self.mode, self.pool, self.embedder,
                           {"run_id": self.run_id, "adjudicator": self.adjudicator})

    # -- commands -------------------------------------------------------
    def cmd_remember(self, rest: str):
        m = re.match(r"^(\S+)\s+(\S+)\s*(=|equals|prefers|forbids)\s*(.+)$", rest.strip())
        if not m:
            print(col("  usage: remember <role> <attribute> = <value>", "d"))
            return
        role, attr, pred, raw = m.groups()
        pred = "equals" if pred == "=" else pred
        if role not in ROLE_TIERS:
            print(col(f"  unknown role {role!r}. try: roles", "r"))
            return

        key = attr if attr.count(":") == 2 else normalize("trip", "1", attr)
        value = parse_value(raw)
        claim = Claim(self.workspace, key, pred, f"{attr} {pred} {raw.strip()}",
                      value, f"{role}-live", role, 0.7)

        res = self.memory.remember(claim)
        if res.error:
            print(col(f"  error: {res.error}", "r"))
            return

        tier = ROLE_TIERS[role]
        print(f"  {col(res.resolution.upper(), RESOLUTION_COLOUR.get(res.resolution, 'd'))}"
              f"  {col(f'({role}, authority tier {tier})', 'd')}")
        for c in res.conflicts:
            det = "tier-1 structural" if c.detector.startswith("tier1") else "tier-2 semantic"
            sim = f" similarity {c.similarity:.2f}" if c.similarity is not None else ""
            print(f"    {col('detected', 'B')} {c.verdict} via {det}{col(sim, 'd')}")
            print(f"    {col('rule', 'B')} {c.policy_rule}: {c.rationale}")
        if res.retries:
            print(col(f"    {res.retries} x 40001 retry (concurrent writer)", "d"))
        if not res.conflicts:
            print(col("    no conflicting neighbour found", "d"))

    def cmd_act(self, rest: str):
        m = re.match(r"^(\S+)\s+(?:needs|requires)\s+(.+)$", rest.strip())
        if not m:
            print(col("  usage: act <action> needs <attribute>", "d"))
            return
        action_type, keys_raw = m.groups()
        keys = tuple(
            k if k.count(":") == 2 else normalize("trip", "1", k)
            for k in (s.strip() for s in keys_raw.split(",")) if k
        )
        res = self.memory.act(Action(self.workspace, "console-1", action_type, {}, keys))
        if res.allowed:
            print(f"  {col('ALLOWED', 'g')} — {action_type} executed, "
                  f"justified by {len(res.justifying_atom_ids)} atom(s)")
        else:
            print(f"  {col('BLOCKED', 'r')} — {res.gate_result}")
            print(f"    {res.reason}")
            if not self.memory.has_action_gate:
                print(col("    (this mode has no action gate; blocked only because "
                          "memory was missing)", "d"))

    def cmd_show(self):
        with self.pool.connection() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT subject_key, object_text, writer_role, status, "
                    "valid_to IS NULL, confidence, evidence_count "
                    "FROM memory_atom WHERE workspace_id = %s ORDER BY valid_from",
                    (self.workspace,))
                rows = cur.fetchall()
        if not rows:
            print(col("  memory is empty", "d"))
            return
        live = [r for r in rows if r[4]]
        by_key: dict = {}
        for r in live:
            by_key.setdefault(r[0], []).append(r)
        for key, atoms in by_key.items():
            active = [a for a in atoms if a[3] == "active"]
            flag = ""
            if any(a[3] == "contested" for a in atoms):
                flag = col("  <- CONTESTED", "r")
            elif len(active) > 1:
                flag = col("  <- TWO LIVE ANSWERS", "r")
            print(f"  {col(key, 'B')}{flag}")
            for a in atoms:
                colour = {"active": "g", "contested": "r", "rejected": "y"}.get(a[3], "d")
                print(f"    {col(a[3], colour):<22} {a[1]}  {col(f'({a[2]})', 'd')}")
        dead = [r for r in rows if not r[4]]
        for r in dead:
            print(col(f"    superseded             {r[1]}  ({r[2]})", "d"))

    def cmd_conflicts(self):
        with self.pool.connection() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT detector, verdict, resolution, policy_rule, rationale "
                    "FROM memory_conflict WHERE workspace_id = %s ORDER BY detected_at",
                    (self.workspace,))
                rows = cur.fetchall()
        if not rows:
            print(col("  nothing detected yet", "d"))
            return
        for det, verdict, resolution, rule, rationale in rows:
            t = "tier-1" if det.startswith("tier1") else "tier-2"
            print(f"  {t}  {verdict:<14} -> {col(resolution, RESOLUTION_COLOUR.get(resolution,'d')):<20}"
                  f" {col(rule or '', 'd')}")
            print(col(f"        {rationale}", "d"))

    def cmd_mode(self, rest: str):
        rest = rest.strip()
        if not rest:
            print(f"  mode is {col(self.mode, 'B')}   (options: {', '.join(MODES)})")
            return
        if rest not in MODES:
            print(col(f"  unknown mode {rest!r}", "r"))
            return
        self.mode = rest
        self.run_id = uuid.uuid4()
        self._register_run()
        self.memory = self._build()
        info = self.memory.info()
        print(f"  memory layer is now {col(self.mode, 'B')}")
        print(col(f"    transactions: {info['uses_transactions']}   "
                  f"semantic layer: {info['uses_semantic_layer']}   "
                  f"action gate: {info['has_action_gate']}", "d"))
        print(col("    the data stays; only the layer changed. "
                  "re-enter the same claims and compare.", "d"))

    def cmd_roles(self):
        print(col("  role                  authority (lower = more authoritative)", "d"))
        for role, tier in sorted(ROLE_TIERS.items(), key=lambda kv: (kv[1], kv[0])):
            print(f"    {role:<22} tier {tier}")

    def cmd_reset(self):
        with self.pool.connection() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                for t in ("memory_atom", "memory_conflict", "action_log"):
                    cur.execute(f"DELETE FROM {t} WHERE workspace_id = %s", (self.workspace,))
        self.workspace = uuid.uuid4()
        self.run_id = uuid.uuid4()
        self._register_run()
        self.memory = self._build()
        print(col("  fresh workspace", "d"))

    # -- loop -----------------------------------------------------------
    def run(self):
        print(BANNER)
        info = self.memory.info()
        print(col(f"  mode={self.mode}  embeddings={info['embedder']['provider']}  "
                  f"tier-2={self.adjudicator.provider}", "d"))
        print(col(f"  workspace {self.workspace}", "d"))
        print(HELP)

        while True:
            try:
                line = input(col("> ", "b")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            cmd, _, rest = line.partition(" ")
            cmd = cmd.lower()
            try:
                if cmd in ("quit", "exit", "q"):
                    break
                elif cmd in ("help", "?"):
                    print(HELP)
                elif cmd == "remember":
                    self.cmd_remember(rest)
                elif cmd == "act":
                    self.cmd_act(rest)
                elif cmd == "show":
                    self.cmd_show()
                elif cmd == "conflicts":
                    self.cmd_conflicts()
                elif cmd == "mode":
                    self.cmd_mode(rest)
                elif cmd == "roles":
                    self.cmd_roles()
                elif cmd == "reset":
                    self.cmd_reset()
                else:
                    print(col(f"  unknown command {cmd!r} — try help", "d"))
            except Exception as exc:  # a live demo must never crash out
                print(col(f"  error: {type(exc).__name__}: {exc}", "r"))

        self.pool.close()
        print(col("bye", "d"))


def main() -> int:
    try:
        Console().run()
    except SystemExit:
        raise
    except Exception as exc:
        print("could not start:\n  " + explain_connect_failure(exc))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
