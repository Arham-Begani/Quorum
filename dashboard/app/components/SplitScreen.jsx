"use client";

const MODE_BLURB = {
  naive: "separate vector store + rows · no transaction · no semantic layer",
  txn_only: "CockroachDB · SERIALIZABLE · no semantic layer",
  quorum: "CockroachDB · SERIALIZABLE · full semantic layer + action gate",
};

function statusOf(verdict) {
  if (!verdict) return { cls: "", label: "—" };
  if (verdict.blocked) return { cls: "blocked", label: "NOT TESTED" };
  return verdict.pass
    ? { cls: "ok", label: "as expected" }
    : { cls: "bad", label: "unexpected" };
}

function Atoms({ atoms }) {
  const live = atoms.filter((a) => !a.valid_to);
  const dead = atoms.filter((a) => a.valid_to);
  const show = [...live, ...dead];
  if (!show.length) return <p className="empty">no memory written</p>;
  return (
    <ul className="atoms">
      {show.map((a, i) => (
        <li key={i}>
          <span className={`pill ${a.status}`}>{a.status}</span>
          <span className="atom-text">{a.object_text}</span>
          <div className="atom-meta">
            {a.subject_key} · {a.writer_role} · confidence {a.confidence}
            {a.superseded_by ? " · superseded" : ""}
          </div>
        </li>
      ))}
    </ul>
  );
}

export default function SplitScreen({ scenario, modes }) {
  return (
    <>
      <div className="split">
        {modes.map((mode) => {
          const m = scenario.modes[mode];
          if (!m) return null;
          const rep = m.report;
          const a = rep.anomalies;
          const perf = rep.performance;
          const v = scenario.verdicts?.[mode];
          const st = statusOf(v);
          const bad = a.contradictory_active_pairs > 0 || a.wrong_actions > 0;
          const action = m.actions?.[m.actions.length - 1];

          return (
            <section key={mode} className={`mode-card ${bad ? "bad" : "ok"}`}>
              <header>
                <h3>{mode}</h3>
                <span className={`verdict ${st.cls}`}>{st.label}</span>
              </header>
              <div className="body">
                <div className="atom-meta" style={{ marginTop: 0, marginBottom: 12 }}>
                  {MODE_BLURB[mode]}
                </div>

                <div className={`outcome ${bad ? "bad" : "ok"}`}>
                  <span className="label">resulting action</span>
                  {action ? (
                    action.executed ? (
                      <>
                        <strong>{action.action_type}</strong> executed
                        {bad && <> — {scenario.wrong_action_note}</>}
                        {!bad && <> against consistent memory</>}
                      </>
                    ) : (
                      <>
                        <strong>{action.action_type}</strong> BLOCKED —{" "}
                        {action.outcome}
                      </>
                    )
                  ) : (
                    "no action recorded"
                  )}
                </div>

                <div className="metrics">
                  <div className={`metric ${a.contradictory_active_pairs ? "bad" : "good"}`}>
                    <div className="n">{a.contradictory_active_pairs}</div>
                    <div className="k">contradictory active pairs</div>
                  </div>
                  <div className={`metric ${a.wrong_actions ? "bad" : "good"}`}>
                    <div className="n">{a.wrong_actions}</div>
                    <div className="k">wrong actions</div>
                  </div>
                  <div className={`metric ${a.blocked_actions ? "warn" : ""}`}>
                    <div className="n">{a.blocked_actions}</div>
                    <div className="k">blocked by gate</div>
                  </div>
                  <div className={`metric ${a.contested_atoms ? "warn" : ""}`}>
                    <div className="n">{a.contested_atoms}</div>
                    <div className="k">contested atoms</div>
                  </div>
                  <div className="metric">
                    <div className="n">{perf.txn_retries}</div>
                    <div className="k">40001 retries</div>
                  </div>
                  <div className="metric">
                    <div className="n">{perf.p50_write_ms}</div>
                    <div className="k">p50 write ms</div>
                  </div>
                </div>

                <span className="label atom-meta">final memory state</span>
                <Atoms atoms={m.atoms} />
              </div>
            </section>
          );
        })}
      </div>

      <div className="section" style={{ marginTop: 16 }}>
        <h3>Why this is not just the database working as designed</h3>
        <p className="sub">
          The middle column is the argument. <code>txn_only</code> is CockroachDB used
          correctly: serializable isolation, zero lost updates, zero dirty reads, zero
          write skew. It still ends up holding two mutually contradictory facts that are
          both currently true, because the contradiction lives across two structurally
          unrelated rows and no isolation level has an opinion about semantics.
          Serializability constrains the <em>order</em> of operations, not the{" "}
          <em>meaning</em> of the data they write. Isolation is necessary. It is not
          sufficient.
        </p>
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>mode</th>
                <th>transactions</th>
                <th>semantic layer</th>
                <th>action gate</th>
                <th>outcome</th>
              </tr>
            </thead>
            <tbody>
              {modes.map((mode) => {
                const m = scenario.modes[mode];
                if (!m) return null;
                const mem = m.report.memory || {};
                const exp = scenario.expectations?.[mode];
                return (
                  <tr key={mode}>
                    <td className="mono">{mode}</td>
                    <td>{mem.uses_transactions ? "yes" : "no"}</td>
                    <td>{mem.uses_semantic_layer ? "yes" : "no"}</td>
                    <td>{mem.has_action_gate ? "yes" : "no"}</td>
                    <td>{exp?.note}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
