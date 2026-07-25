# Managed MCP Server — the read-only auditor

CockroachDB Cloud's **Managed MCP Server** lets a human open Claude Code, attach
to the cluster, and interrogate memory state and the conflict log directly. In
Quorum that persona is an **auditor**: read-only, audit-logged, and unable to
change a single atom.

That is not a limitation to apologise for. An auditor that can write is not an
auditor. Read-only-by-default with audit logging on is the documented posture,
and it is the part of the Product Readiness story most entrants will skip.

## Setup

1. In the CockroachDB Cloud Console, open your cluster → **Connect** → **MCP**.
   Copy the generated config snippet.
2. Create the read-only SQL user first (see `infra/ccloud/provision.sh`, which
   creates `auditor` with `SELECT` and nothing else):

   ```sql
   CREATE USER auditor WITH PASSWORD '<generated>';
   GRANT CONNECT ON DATABASE quorum TO auditor;
   GRANT SELECT ON ALL TABLES IN SCHEMA public TO auditor;
   ```

3. Add the server to Claude Code. Credentials come from the environment, never
   from the committed file:

   ```jsonc
   // .mcp.json  — commit this shape, never the real values
   {
     "mcpServers": {
       "quorum-crdb": {
         "command": "npx",
         "args": ["-y", "@cockroachlabs/mcp-server"],
         "env": {
           "CRDB_URL": "${CRDB_URL_AUDITOR}",
           "CRDB_READ_ONLY": "true"
         }
       }
     }
   }
   ```

4. Verify the posture before going on camera. The auditor must be able to read
   and must fail to write:

   ```sql
   SELECT count(*) FROM memory_atom;                        -- succeeds
   UPDATE memory_atom SET status = 'active' WHERE true;      -- must fail
   ```

   If the `UPDATE` succeeds, the account is not read-only and the demo claim is
   false. Fix it before recording.

## Why this is load-bearing rather than decorative

The audit story is the answer to "how would you actually operate this?" A human
investigating a blocked booking needs to ask questions of live memory without
being able to perturb it. `quorum/mcp/queries.md` holds the exact questions,
rehearsed, so the live segment is not a coin flip.

## Fallback if the Managed MCP Server is unavailable

Point Claude Code at the read-only FastAPI surface instead
(`uvicorn quorum.api.server:app`) — it performs no writes by construction, since
the only write path in the system is `run_txn` inside the memory client. Say so
plainly rather than implying MCP was used when it was not.
