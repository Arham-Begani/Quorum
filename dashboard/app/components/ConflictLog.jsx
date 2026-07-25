"use client";

/** Explainability made visible: what was compared, by which tier, what the
 *  verdict was, which policy rule fired, and why. */
export default function ConflictLog({ scenario, modes }) {
  const rows = [];
  modes.forEach((mode) => {
    const m = scenario.modes[mode];
    if (!m) return;
    (m.conflicts || []).forEach((c) => rows.push({ mode, ...c }));
  });

  return (
    <div className="section">
      <h3>Conflict log</h3>
      <p className="sub">
        Every detection is written to <code>memory_conflict</code>, including benign
        ones. The ratio of benign to contradictory detections is itself a credibility
        signal — a system that only ever reports contradictions is not detecting, it is
        alarming. <code>naive</code> and <code>txn_only</code> produce no rows here at
        all, because neither has a detection layer.
      </p>
      {rows.length === 0 ? (
        <p className="empty">
          No detections recorded for this scenario. Only <code>quorum</code> detects.
        </p>
      ) : (
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>mode</th>
                <th>subject key</th>
                <th>detector</th>
                <th>similarity</th>
                <th>verdict</th>
                <th>resolution</th>
                <th>rule</th>
                <th>rationale</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((c, i) => (
                <tr key={i}>
                  <td className="mono">{c.mode}</td>
                  <td className="mono">{c.subject_key}</td>
                  <td className="mono">
                    {c.detector === "tier1_structural" ? "tier 1" : "tier 2"}
                  </td>
                  <td className="mono">
                    {c.similarity != null ? Number(c.similarity).toFixed(3) : "—"}
                  </td>
                  <td className="mono">{c.verdict}</td>
                  <td className="mono">{c.resolution}</td>
                  <td className="mono">{c.policy_rule}</td>
                  <td>{c.rationale}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="section" style={{ marginTop: 18, background: "var(--panel-2)" }}>
        <h3>Resolution policy</h3>
        <p className="sub">
          Ordered rules, first match wins. Rule-based rather than LLM-based on purpose:
          an LLM that merges contradictory facts is unreliable, unexplainable, and
          destroys the contest path, which is the best safety story here.
        </p>
        <table>
          <tbody>
            <tr><td className="mono">R1 AUTHORITY</td><td>higher authority tier wins — supersede, or reject the incoming claim</td></tr>
            <tr><td className="mono">R2 EVIDENCE</td><td>materially more corroborated claim wins</td></tr>
            <tr><td className="mono">R3 RECENCY</td><td>within a tier, a materially more confident newer claim supersedes</td></tr>
            <tr><td className="mono">R4 CONTEST</td><td>otherwise mark both contested and block dependent actions — declining to guess is the feature</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}
