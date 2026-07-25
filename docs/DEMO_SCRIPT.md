# Demo script — target 2:50

The rules cap the video at 3 minutes and require it to show **the memory layer**,
not the app. The `txn_only` beat at 0:40 is the most important thirty seconds in
the submission: it is what separates this from every other entry that will show
"vector store bad, CockroachDB good".

| time | shot | line to land |
|---|---|---|
| 0:00–0:15 | Dashboard header, S1 selected, three columns visible | "Four agents. One shared memory. They disagree." |
| 0:15–0:40 | `naive` column. Both check-in dates sitting side by side, both `active`. The red booking callout. | "Both facts committed. The agent believed both, and booked the wrong night." |
| 0:40–1:10 | `txn_only` column. Point at the capability row: transactions **yes**, semantic layer **no**. Same red callout. | "This is CockroachDB, used correctly. Serializable. Zero lost updates, zero dirty reads. And the same wrong booking. Isolation is necessary. It is not sufficient." |
| 1:10–1:55 | `quorum` column: one atom superseded, gate green. Switch to S3 — contest, `book_transfer` BLOCKED. Then the Conflict log tab: detector tier, verdict, R4, rationale. | "The check happens inside the transaction that commits the write. That is only sound because it is serializable. And when the policy can't justify a winner, it refuses to guess — and refuses to act." |
| 1:55–2:20 | Claude Code + MCP, read-only. Query 1 then query 5 from `quorum/mcp/queries.md`. Then the forensic timeline scrubber. | "Read-only, audit-logged. And we can replay exactly what it knew at the instant it decided." |
| 2:20–2:40 | Terminal: `make test-isolation` — 100 races, zero violations, real 40001 count. Optionally the node-kill run. | "A hundred concurrent races. Never two contradictory truths. The retries are the database refusing to break serializability." |
| 2:40–2:50 | Architecture diagram, tool and service callouts on screen. | Name the three CockroachDB tools and the AWS services. |

## Preparation

```bash
python -m quorum.harness.report --all --delay-ms 40   # fresh numbers
python -m quorum.harness.export_demo                  # bake the snapshot
cd dashboard && npm run build && npx serve out        # or npm run dev
```

Have a second terminal ready with `make test-isolation` already scrolled to the
top, and Claude Code already attached to the MCP server with the queries pasted
in — an unrehearsed live MCP query on camera is a coin flip.

## Things to say precisely

- **Do not** say "the vector index finds the contradiction" while showing S1.
  At demo scale the exact `subject_key` lookup does that work. The vector index
  is what generalises to claims that contradict *without* sharing a key, which is
  S2. Say that instead, and say it accurately.
- **Do** say the race window in the spike is artificially widened, and that the
  control run with it removed produced the same result. Volunteering it is worth
  more than being asked.
- **Do** say `txn_only`'s failure is analytically certain, not a surprise. The
  surprising result is that it is *unavoidable* without a semantic layer.
- If running without Bedrock, say so on camera. The dashboard already shows the
  banner; do not talk over it.

## The single most likely judge question

> "Isn't this just the database working as designed?"

> "Yes — that is the middle column, and it still produces the wrong booking.
> Serializability constrains the order of operations. It says nothing about the
> meaning of the data they write. Two INSERTs of contradictory facts touch
> different rows and violate no constraint, so they are trivially serializable in
> either order. The gap isn't a bug in CockroachDB; it's a layer that doesn't
> exist yet. That's what Quorum is."
