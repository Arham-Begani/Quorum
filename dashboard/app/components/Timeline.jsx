"use client";

import { useMemo, useState } from "react";

/** Forensic view: what did the swarm believe at instant T, and what did it do
 *  because of that? Reconstructed from the append-only validity intervals, which
 *  is the same information AS OF SYSTEM TIME returns from the cluster — the API
 *  endpoint /timeline/{run_id}?at=… serves the live version. */
export default function Timeline({ scenario, modes }) {
  const [mode, setMode] = useState("quorum");
  const m = scenario.modes[mode];

  const events = useMemo(() => {
    if (!m) return [];
    const e = [];
    (m.atoms || []).forEach((a) => {
      e.push({ t: a.valid_from, kind: "write", atom: a });
      if (a.valid_to) e.push({ t: a.valid_to, kind: "close", atom: a });
    });
    (m.actions || []).forEach((a) => e.push({ t: a.created_at, kind: "action", action: a }));
    return e.sort((x, y) => new Date(x.t) - new Date(y.t));
  }, [m]);

  const [idx, setIdx] = useState(0);
  const safeIdx = Math.min(idx, Math.max(0, events.length - 1));
  const at = events.length ? events[safeIdx].t : null;

  const believed = useMemo(() => {
    if (!m || !at) return [];
    const cutoff = new Date(at);
    return (m.atoms || []).filter(
      (a) =>
        new Date(a.valid_from) <= cutoff && (!a.valid_to || new Date(a.valid_to) > cutoff)
    );
  }, [m, at]);

  if (!m) return <p className="empty">no run for this mode</p>;

  return (
    <div className="section rise">
      <h3>Forensic timeline</h3>
      <p className="sub">
        Drag to move through the run. The left panel shows exactly what memory
        held at that instant — which is how you answer{" "}
        <em>&ldquo;what did the swarm believe right before it made the
        booking?&rdquo;</em> This works because memory is append-only: nothing is
        ever deleted, only closed out with <code>valid_to</code> and{" "}
        <code>superseded_by</code>. Against a live cluster the same question is
        answered by <code>AS OF SYSTEM TIME</code>, which is only possible because{" "}
        <code>gc.ttlseconds</code> was raised at provisioning time.
      </p>

      <div className="picker" style={{ marginBottom: 18 }}>
        <span className="picker-label">mode</span>
        {modes.map((mo) => (
          <button
            key={mo}
            data-active={mo === mode}
            onClick={() => { setMode(mo); setIdx(0); }}
          >
            {mo}
          </button>
        ))}
      </div>

      <div className="tl-controls">
        <input
          type="range"
          min={0}
          max={Math.max(0, events.length - 1)}
          value={safeIdx}
          onChange={(e) => setIdx(Number(e.target.value))}
          aria-label="Scrub through the run"
        />
        <span className="ts">{at ? new Date(at).toISOString().replace("T", " ").slice(0, 23) : "—"}</span>
      </div>

      <div className="tl-grid">
        <div>
          <span className="eyebrow">believed at this instant</span>
          {believed.length ? (
            <ul className="atoms">
              {believed.map((a, i) => (
                <li key={i} data-s={a.status}>
                  <div>
                    <span className="pill" data-s={a.status}>{a.status}</span>
                    <span className="atom-text">{a.object_text}</span>
                  </div>
                  <div className="atom-meta">
                    {a.subject_key} · {a.writer_role}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className="empty">memory was empty</p>
          )}
        </div>

        <div>
          <span className="eyebrow">event log</span>
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th className="num">t</th>
                  <th>event</th>
                  <th>detail</th>
                </tr>
              </thead>
              <tbody>
                {events.map((e, i) => (
                  <tr key={i} data-future={i > safeIdx} data-now={i === safeIdx}>
                    <td className="num">{new Date(e.t).toISOString().slice(11, 23)}</td>
                    <td>
                      <span
                        className="chip"
                        data-tone={
                          e.kind === "action"
                            ? e.action.executed ? "good" : "warn"
                            : null
                        }
                      >
                        {e.kind}
                      </span>
                    </td>
                    <td>
                      {e.kind === "action" ? (
                        <>
                          <strong>{e.action.action_type}</strong>{" "}
                          <span className="dim">· {e.action.gate_result}</span>
                          {e.action.outcome ? (
                            <span className="dim"> · {e.action.outcome}</span>
                          ) : null}
                          {e.action.justifying_atom_ids?.length > 0 && (
                            <div className="atom-meta">
                              justified by {e.action.justifying_atom_ids.length} atom(s)
                            </div>
                          )}
                        </>
                      ) : (
                        <>
                          {e.atom.object_text}
                          <div className="atom-meta">
                            {e.atom.writer_role} · {e.atom.subject_key}
                          </div>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}
