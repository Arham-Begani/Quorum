"use client";

/* The screen the video is built around.
 *
 * Design intent: colour encodes OUTCOME, never mode identity. `naive` and
 * `txn_only` must look equally wrong, because they are — while the capability
 * strip shows that txn_only has everything naive lacks. That contrast, held in
 * one frame, is the entire argument:
 *
 *     transactions ✓   semantic layer ✗   →  still books the wrong night
 */

const BLURB = {
  naive: "separate vector store + rows\nno transaction · no semantics",
  txn_only: "CockroachDB · SERIALIZABLE\nno semantic layer",
  quorum: "CockroachDB · SERIALIZABLE\nfull semantics + action gate",
};

const CAPS = [
  ["txn", "uses_transactions"],
  ["semantics", "uses_semantic_layer"],
  ["gate", "has_action_gate"],
];

function verdictOf(v) {
  if (!v) return { tone: null, label: "—" };
  if (v.blocked) return { tone: "skip", label: "not tested" };
  return v.pass
    ? { tone: "ok", label: "as expected" }
    : { tone: "bad", label: "unexpected" };
}

/* Tone is derived from consequence, in severity order. */
function toneOf(a) {
  if (a.wrong_actions > 0 || a.contradictory_active_pairs > 0) return "crit";
  if (a.blocked_actions > 0 || a.contested_atoms > 0) return "warn";
  return "good";
}

function Consequence({ tone, action, scenario }) {
  const glyph = tone === "crit" ? "✕" : tone === "warn" ? "⊘" : "✓";

  let line = "no action recorded";
  let note = null;

  if (action) {
    if (action.executed) {
      line = `${action.action_type} executed`;
      note =
        tone === "crit"
          ? scenario.wrong_action_note
          : "against memory the policy engine could justify";
    } else {
      line = `${action.action_type} blocked`;
      note =
        action.outcome ||
        "the gate refused to act on contested memory — declining to guess is the feature";
    }
  }

  return (
    <div className="consequence" data-tone={tone}>
      <span className="eyebrow">resulting action</span>
      <div className="consequence-line">
        <span className="glyph">{glyph}</span>
        {line}
      </div>
      {note && <p className="consequence-note">{note}</p>}
    </div>
  );
}

function Atoms({ atoms }) {
  const live = atoms.filter((a) => !a.valid_to);
  const dead = atoms.filter((a) => a.valid_to);
  const show = [...live, ...dead];
  if (!show.length) return <p className="empty">no memory written</p>;

  return (
    <ul className="atoms">
      {show.map((a, i) => (
        <li key={i} data-s={a.status}>
          <div>
            <span className="pill" data-s={a.status}>{a.status}</span>
            <span className="atom-text">{a.object_text}</span>
          </div>
          <div className="atom-meta">
            {a.subject_key} · {a.writer_role} · conf {a.confidence}
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
        {modes.map((mode, i) => {
          const m = scenario.modes[mode];
          if (!m) return null;

          const a = m.report.anomalies;
          const perf = m.report.performance;
          const mem = m.report.memory || {};
          const tone = toneOf(a);
          const v = verdictOf(scenario.verdicts?.[mode]);
          const action = m.actions?.[m.actions.length - 1];

          return (
            <section
              key={mode}
              className="panel rise"
              data-tone={tone}
              style={{ animationDelay: `${300 + i * 110}ms` }}
            >
              <div className="panel-head">
                <div className="panel-mode">
                  <h3>{mode}</h3>
                  <span className="verdict" data-tone={v.tone}>{v.label}</span>
                </div>
                <p className="panel-blurb" style={{ whiteSpace: "pre-line" }}>
                  {BLURB[mode]}
                </p>
                <div className="caps">
                  {CAPS.map(([label, key]) => (
                    <span key={key} className="cap" data-on={!!mem[key]}>
                      <span className="mark">{mem[key] ? "✓" : "✗"}</span>
                      {label}
                    </span>
                  ))}
                </div>
              </div>

              <div className="panel-body">
                <Consequence tone={tone} action={action} scenario={scenario} />

                <div className="tiles">
                  <div className="tile" data-tone={a.contradictory_active_pairs ? "crit" : "good"}>
                    <div className="v">{a.contradictory_active_pairs}</div>
                    <div className="k">Contradictory pairs</div>
                  </div>
                  <div className="tile" data-tone={a.wrong_actions ? "crit" : "good"}>
                    <div className="v">{a.wrong_actions}</div>
                    <div className="k">Wrong actions</div>
                  </div>
                  <div className="tile" data-tone={a.blocked_actions ? "warn" : null}>
                    <div className="v">{a.blocked_actions}</div>
                    <div className="k">Blocked by gate</div>
                  </div>
                  <div className="tile" data-tone={a.contested_atoms ? "warn" : null}>
                    <div className="v">{a.contested_atoms}</div>
                    <div className="k">Contested atoms</div>
                  </div>
                  <div className="tile">
                    <div className="v">{perf.txn_retries}</div>
                    <div className="k">40001 retries</div>
                  </div>
                  <div className="tile">
                    <div className="v">{perf.p50_write_ms}</div>
                    <div className="k">p50 write ms</div>
                  </div>
                </div>

                <div>
                  <span className="eyebrow">final memory state</span>
                  <Atoms atoms={m.atoms} />
                </div>

                {mode === "txn_only" && (
                  <div className="pivot-note">
                    <span className="eyebrow">the pivot</span>
                    <p>
                      Serializable. Zero lost updates, zero dirty reads, zero
                      write skew. And the same wrong booking as the column on the
                      left.
                    </p>
                  </div>
                )}
              </div>
            </section>
          );
        })}
      </div>

      <div className="section rise" style={{ animationDelay: "640ms" }}>
        <div className="argument">
          <blockquote className="pullquote">
            <span className="lead">Isn&rsquo;t this just the database working as designed?</span>
            Yes — that is the middle column, and it still produces the wrong
            booking. Serializability constrains the <em>order</em> of operations.
            It says nothing about the <em>meaning</em> of the data they write.
          </blockquote>

          <div>
            <p className="sub" style={{ marginTop: 0 }}>
              Two INSERTs of contradictory facts touch different rows and violate
              no constraint, so they are trivially serializable in either order.
              The gap is not a bug in CockroachDB; it is a layer that does not
              exist yet. That layer is what Quorum is.
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
                    const tone = toneOf(m.report.anomalies);
                    return (
                      <tr key={mode}>
                        <td className="mono">{mode}</td>
                        {CAPS.map(([, key]) => (
                          <td key={key}>
                            <span className="chip" data-tone={mem[key] ? "good" : null}>
                              {mem[key] ? "✓ yes" : "✗ no"}
                            </span>
                          </td>
                        ))}
                        <td>
                          <span className="chip" data-tone={tone}>
                            {tone === "crit" ? "✕ wrong" : tone === "warn" ? "⊘ blocked" : "✓ safe"}
                          </span>
                          <div className="dim" style={{ marginTop: 6, fontSize: 12 }}>
                            {exp?.note}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
