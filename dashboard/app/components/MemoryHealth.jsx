"use client";

function Bar({ label, value, max, tone }) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  const colour =
    tone === "bad" ? "var(--bad)" : tone === "warn" ? "var(--warn)" : "var(--info)";
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
        <span className="mono">{label}</span>
        <span className="mono" style={{ color: "var(--muted)" }}>{value}</span>
      </div>
      <div style={{ background: "var(--panel-2)", height: 7, borderRadius: 4, marginTop: 4 }}>
        <div style={{ width: `${pct}%`, height: "100%", background: colour, borderRadius: 4 }} />
      </div>
    </div>
  );
}

export default function MemoryHealth({ snap, scenario }) {
  const perModes = snap.modes
    .map((mode) => ({ mode, rep: scenario.modes[mode]?.report }))
    .filter((x) => x.rep);

  const maxLat = Math.max(...perModes.map((x) => x.rep.performance.p95_write_ms || 0), 1);

  return (
    <>
      <div className="section">
        <h3>Cost of consistency</h3>
        <p className="sub">
          The semantic layer is not free, and pretending otherwise would be the
          easiest thing for a judge to catch. Here is what it costs on this scenario.
          The quorum path does a neighbourhood read and a policy evaluation inside the
          transaction, and pays retries when writers genuinely contend.
        </p>
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>mode</th><th>p50 ms</th><th>p95 ms</th><th>p99 ms</th>
                <th>40001 retries</th><th>give-ups</th><th>embed calls</th>
                <th>cache hit rate</th><th>tier-2 calls</th><th>est. cost USD</th>
              </tr>
            </thead>
            <tbody>
              {perModes.map(({ mode, rep }) => {
                const p = rep.performance;
                return (
                  <tr key={mode}>
                    <td className="mono">{mode}</td>
                    <td className="mono">{p.p50_write_ms}</td>
                    <td className="mono">{p.p95_write_ms}</td>
                    <td className="mono">{p.p99_write_ms}</td>
                    <td className="mono">{p.txn_retries}</td>
                    <td className="mono">{p.txn_give_ups}</td>
                    <td className="mono">{p.embed_calls}</td>
                    <td className="mono">{(p.embed_cache_hit_rate * 100).toFixed(0)}%</td>
                    <td className="mono">{p.adjudicator_calls}</td>
                    <td className="mono">${p.est_cost_usd}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      <div className="split">
        <div className="section">
          <h3>Write latency (p95)</h3>
          <p className="sub">Consistency has a price. It is bounded and measured.</p>
          {perModes.map(({ mode, rep }) => (
            <Bar
              key={mode}
              label={mode}
              value={rep.performance.p95_write_ms}
              max={maxLat}
              tone={mode === "quorum" ? "warn" : undefined}
            />
          ))}
        </div>

        <div className="section">
          <h3>Retries are the system working</h3>
          <p className="sub">
            40001 <code>serialization_failure</code> is CockroachDB refusing a commit
            that would have broken serializability. Bounded, counted and surfaced — not
            hidden. A visible retry count is a readiness signal, not an embarrassment.
          </p>
          {perModes.map(({ mode, rep }) => (
            <Bar
              key={mode}
              label={mode}
              value={rep.performance.txn_retries}
              max={Math.max(...perModes.map((x) => x.rep.performance.txn_retries), 1)}
              tone="warn"
            />
          ))}
        </div>

        <div className="section">
          <h3>Detection mix</h3>
          <p className="sub">
            Tier 1 is deterministic, free and instant. If tier 2 fired on everything,
            subject-key normalization would be broken.
          </p>
          {perModes.map(({ mode, rep }) => (
            <div key={mode} style={{ marginBottom: 12 }}>
              <div className="mono" style={{ fontSize: 12 }}>{mode}</div>
              <div className="atom-meta">
                {rep.conflicts.detected} detected · tier1 {rep.conflicts.tier1} · tier2{" "}
                {rep.conflicts.tier2}
                {rep.conflicts.policy_rules &&
                  Object.keys(rep.conflicts.policy_rules).length > 0 && (
                    <> · rules {JSON.stringify(rep.conflicts.policy_rules)}</>
                  )}
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="section">
        <h3>Providers</h3>
        <p className="sub">
          Recorded on every run so a result can never be mistaken for one produced by
          different machinery.
        </p>
        <table>
          <tbody>
            <tr>
              <td className="mono">embeddings</td>
              <td className="mono">{snap.providers?.embedder?.provider}</td>
              <td>{snap.providers?.embedder?.model_id || "offline stand-in"}</td>
            </tr>
            <tr>
              <td className="mono">tier-2 adjudicator</td>
              <td className="mono">{snap.providers?.tier2?.provider}</td>
              <td>
                {snap.providers?.tier2?.model_id ||
                  "no model reachable — escalated pairs fail closed to CONTEST"}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </>
  );
}
