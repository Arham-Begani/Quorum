"use client";

/* Meters, not charts: each is a single magnitude per mode, and a bar chart of
 * three bars is a table with extra steps. Meter spec — fill carries severity,
 * the unfilled track is a lighter step of the same ramp, 4px rounded data-end
 * square at the baseline. Values are labelled directly, so the meter is a
 * visual aid rather than the only way to read the number. */

function Meter({ label, value, max, tone = "good", unit }) {
  /* A zero must render as an empty track. Giving 0 a minimum-width nub (so it
   * stays "visible") states a value that is not there — the number beside it
   * already says zero. Only non-zero values get the 2% legibility floor. */
  const pct =
    value > 0 && max > 0 ? Math.max(2, Math.min(100, (value / max) * 100)) : 0;
  return (
    <div className="meter">
      <div className="meter-head">
        <span className="meter-label">{label}</span>
        <span className="meter-value">
          {value}
          {unit && <span className="unit">{unit}</span>}
        </span>
      </div>
      <div className="meter-track" data-tone={tone}>
        <div className="meter-fill" data-tone={tone} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

/* BEDROCK_CHAT_MODEL_ID in .env is empty followed by an inline comment, and the
 * comment survives into the snapshot as the model id — so the provenance table
 * was rendering "# confirm what is enabled in YOUR region" as though it were a
 * model name. Treat a commented or blank id as unset. The .env parsing itself
 * still wants fixing upstream; this stops it reaching the demo URL. */
function modelId(p) {
  const v = (p?.model_id || "").trim();
  if (!v || v.startsWith("#")) return null;
  return v;
}

export default function MemoryHealth({ snap, scenario }) {
  const perModes = snap.modes
    .map((mode) => ({ mode, rep: scenario.modes[mode]?.report }))
    .filter((x) => x.rep);

  const maxLat = Math.max(...perModes.map((x) => x.rep.performance.p95_write_ms || 0), 1);
  const maxRetry = Math.max(...perModes.map((x) => x.rep.performance.txn_retries || 0), 1);

  return (
    <>
      <div className="section rise">
        <h3>The cost of consistency</h3>
        <p className="sub">
          The semantic layer is not free, and pretending otherwise would be the
          easiest thing for a judge to catch. The quorum path does a neighbourhood
          read and a policy evaluation <em>inside</em> the transaction, and pays
          retries when writers genuinely contend. Here is what that costs on this
          scenario.
        </p>
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>mode</th>
                <th className="num">p50 ms</th>
                <th className="num">p95 ms</th>
                <th className="num">p99 ms</th>
                <th className="num">40001</th>
                <th className="num">give-ups</th>
                <th className="num">embed calls</th>
                <th className="num">cache hits</th>
                <th className="num">tier-2 calls</th>
                <th className="num">est. cost</th>
              </tr>
            </thead>
            <tbody>
              {perModes.map(({ mode, rep }) => {
                const p = rep.performance;
                return (
                  <tr key={mode}>
                    <td className="mono">{mode}</td>
                    <td className="num">{p.p50_write_ms}</td>
                    <td className="num">{p.p95_write_ms}</td>
                    <td className="num">{p.p99_write_ms}</td>
                    <td className="num">{p.txn_retries}</td>
                    <td className="num">{p.txn_give_ups}</td>
                    <td className="num">{p.embed_calls}</td>
                    <td className="num">{(p.embed_cache_hit_rate * 100).toFixed(0)}%</td>
                    <td className="num">{p.adjudicator_calls}</td>
                    <td className="num">${p.est_cost_usd}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      <div className="split">
        <div className="section rise" style={{ animationDelay: "80ms", marginTop: 16 }}>
          <h3>Write latency</h3>
          <p className="sub">
            p95, per mode. Consistency has a price. It is bounded, and it is
            measured.
          </p>
          {perModes.map(({ mode, rep }) => (
            <Meter
              key={mode}
              label={mode}
              value={rep.performance.p95_write_ms}
              max={maxLat}
              unit="ms"
              tone={mode === "quorum" ? "warn" : "good"}
            />
          ))}
        </div>

        <div className="section rise" style={{ animationDelay: "160ms", marginTop: 16 }}>
          <h3>Retries are the system working</h3>
          <p className="sub">
            40001 <code>serialization_failure</code> is CockroachDB refusing a
            commit that would have broken serializability. Bounded, counted and
            surfaced — never hidden.
          </p>
          {perModes.map(({ mode, rep }) => (
            <Meter
              key={mode}
              label={mode}
              value={rep.performance.txn_retries}
              max={maxRetry}
              tone="warn"
            />
          ))}
        </div>

        <div className="section rise" style={{ animationDelay: "240ms", marginTop: 16 }}>
          <h3>Detection mix</h3>
          <p className="sub">
            Tier 1 is deterministic, free and instant. If tier 2 fired on
            everything, subject-key normalization would be broken.
          </p>
          {perModes.map(({ mode, rep }) => (
            <div key={mode} style={{ marginBottom: 14 }}>
              <div className="meter-label" style={{ marginBottom: 4 }}>{mode}</div>
              <div className="atom-meta" style={{ marginTop: 0 }}>
                {rep.conflicts.detected} detected · tier 1 {rep.conflicts.tier1} ·
                tier 2 {rep.conflicts.tier2}
              </div>
              {rep.conflicts.policy_rules &&
                Object.keys(rep.conflicts.policy_rules).length > 0 && (
                  <div style={{ display: "flex", gap: 6, marginTop: 7, flexWrap: "wrap" }}>
                    {Object.entries(rep.conflicts.policy_rules).map(([rule, n]) => (
                      <span key={rule} className="chip" data-tone={rule === "R4" ? "warn" : null}>
                        {rule} × {n}
                      </span>
                    ))}
                  </div>
                )}
            </div>
          ))}
        </div>
      </div>

      <div className="section rise" style={{ animationDelay: "320ms" }}>
        <h3>Provenance</h3>
        <p className="sub">
          Recorded on every run, so a result can never be mistaken for one
          produced by different machinery.
        </p>
        <div className="scroll">
          <table>
            <tbody>
              <tr>
                <td className="mono" style={{ width: 200 }}>embeddings</td>
                <td className="mono">{snap.providers?.embedder?.provider}</td>
                <td className="dim">
                  {modelId(snap.providers?.embedder) || "offline stand-in"}
                </td>
              </tr>
              <tr>
                <td className="mono">tier-2 adjudicator</td>
                <td className="mono">{snap.providers?.tier2?.provider}</td>
                <td className="dim">
                  {modelId(snap.providers?.tier2) ||
                    "no model reachable — escalated pairs fail closed to CONTEST"}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
