"use client";

/** Explainability made visible: what was compared, by which tier, what the
 *  verdict was, which policy rule fired, and why. */

const RULES = [
  ["R1", "authority", "Higher authority tier wins — supersede, or reject the incoming claim."],
  ["R2", "evidence", "The materially better corroborated claim wins."],
  ["R3", "recency", "Within a tier, a materially more confident newer claim supersedes."],
  ["R4", "contest", "Otherwise mark both contested and block dependent actions. Declining to guess is the feature, not a failure mode."],
];

const RES_TONE = {
  contest: "warn",
  reject: "crit",
  supersede: "good",
  reinforce: "good",
  accept: "good",
};

export default function ConflictLog({ scenario, modes }) {
  const rows = [];
  modes.forEach((mode) => {
    const m = scenario.modes[mode];
    if (!m) return;
    (m.conflicts || []).forEach((c) => rows.push({ mode, ...c }));
  });

  return (
    <>
      <div className="section rise">
        <h3>Conflict log</h3>
        <p className="sub">
          Every detection is written to <code>memory_conflict</code>, including
          benign ones. The ratio of benign to contradictory detections is itself a
          credibility signal — a system that only ever reports contradictions is
          not detecting, it is alarming. <code>naive</code> and{" "}
          <code>txn_only</code> produce no rows here at all, because neither has a
          detection layer to produce them.
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
                  <th className="num">similarity</th>
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
                    <td>
                      <span className="chip">
                        {c.detector === "tier1_structural" ? "tier 1" : "tier 2"}
                      </span>
                    </td>
                    <td className="num">
                      {c.similarity != null ? Number(c.similarity).toFixed(3) : "—"}
                    </td>
                    <td className="mono">{c.verdict}</td>
                    <td>
                      <span className="chip" data-tone={RES_TONE[c.resolution] || null}>
                        {c.resolution}
                      </span>
                    </td>
                    <td className="mono">{c.policy_rule}</td>
                    <td>{c.rationale}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="section rise" style={{ animationDelay: "90ms" }}>
        <h3>Resolution policy</h3>
        <p className="sub">
          Ordered rules, first match wins, each a pure function. Rule-based rather
          than LLM-based on purpose: a model that merges contradictory facts is
          unreliable, unexplainable, and destroys the contest path — which is the
          best safety story here.
        </p>
        <div className="scroll">
          <table>
            <tbody>
              {RULES.map(([id, name, text]) => (
                <tr key={id}>
                  <td className="mono" style={{ whiteSpace: "nowrap", width: 1 }}>
                    <span className="chip" data-tone={id === "R4" ? "warn" : null}>
                      {id} {name}
                    </span>
                  </td>
                  <td>{text}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
