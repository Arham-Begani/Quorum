"use client";

import { useEffect, useMemo, useState } from "react";
import SplitScreen from "./components/SplitScreen";
import ConflictLog from "./components/ConflictLog";
import MemoryHealth from "./components/MemoryHealth";
import Timeline from "./components/Timeline";

const TABS = [
  ["split", "Three-mode comparison"],
  ["conflicts", "Conflict log"],
  ["health", "Memory health"],
  ["timeline", "Forensic timeline"],
];

export default function Page() {
  const [snap, setSnap] = useState(null);
  const [err, setErr] = useState(null);
  const [tab, setTab] = useState("split");
  const [scenarioId, setScenarioId] = useState(null);

  useEffect(() => {
    fetch("./demo-snapshot.json")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d) => {
        setSnap(d);
        setScenarioId(d.scenarios?.[0]?.id ?? null);
      })
      .catch((e) => setErr(e.message));
  }, []);

  const scenario = useMemo(
    () => snap?.scenarios?.find((s) => s.id === scenarioId) ?? null,
    [snap, scenarioId]
  );

  if (err) {
    return (
      <div className="wrap">
        <p className="empty">
          Could not load demo-snapshot.json ({err}). Generate it with{" "}
          <code>python -m quorum.harness.export_demo --rerun</code>.
        </p>
      </div>
    );
  }
  if (!snap) return <div className="wrap"><p className="empty">Loading…</p></div>;

  const offline = snap.providers?.embedder?.provider !== "bedrock_titan";

  return (
    <div className="wrap">
      <header className="masthead">
        <h1>
          Quorum<span>.</span> memory consistency for multi-agent systems
        </h1>
        <p className="thesis">
          Transactions solve <em>write</em> conflicts. They do not solve{" "}
          <em>semantic</em> conflicts. Two agents can write mutually contradictory
          facts as two different rows, both commit cleanly under SERIALIZABLE, and
          the swarm now holds memory that is internally inconsistent — and will
          produce a wrong action. Quorum detects contradiction{" "}
          <strong>inside the transaction that commits the write</strong>, resolves
          it under an explicit policy, and refuses to act on contested memory.
        </p>
        <div className="meta">
          {snap.cluster} · snapshot {new Date(snap.generated_at).toUTCString()} ·
          embeddings: {snap.providers?.embedder?.provider} · tier-2:{" "}
          {snap.providers?.tier2?.provider}
        </div>
        {offline && (
          <div className="banner">
            This snapshot was produced <strong>without Bedrock</strong>. Embeddings
            come from a deterministic offline stand-in and tier-2 adjudication fails
            closed rather than classifying. Everything CockroachDB-side — the
            serializable write path, contradiction detection on shared subject keys,
            supersession, contest, and the action gate — is real. Scenarios whose
            conflicting claims do <em>not</em> share a subject key are marked{" "}
            <code>NOT TESTED</code> rather than counted as passing.
          </div>
        )}
      </header>

      <nav className="tabs">
        {TABS.map(([id, label]) => (
          <button key={id} data-active={tab === id} onClick={() => setTab(id)}>
            {label}
          </button>
        ))}
      </nav>

      <div className="scenario-picker">
        {snap.scenarios.map((s) => (
          <button
            key={s.id}
            data-active={s.id === scenarioId}
            onClick={() => setScenarioId(s.id)}
          >
            {s.id}
          </button>
        ))}
      </div>

      {scenario && (
        <>
          <div className="scenario-head">
            <h2>
              {scenario.title}
              <span className="tag">{scenario.tier}</span>
              {scenario.requires_semantic_embeddings && (
                <span className="tag">needs semantic embeddings</span>
              )}
            </h2>
            <p>{scenario.description}</p>
          </div>

          {tab === "split" && <SplitScreen scenario={scenario} modes={snap.modes} />}
          {tab === "conflicts" && <ConflictLog scenario={scenario} modes={snap.modes} />}
          {tab === "health" && <MemoryHealth snap={snap} scenario={scenario} />}
          {tab === "timeline" && <Timeline scenario={scenario} modes={snap.modes} />}
        </>
      )}

      <footer className="foot">
        Quorum · Apache-2.0 · CockroachDB distributed vector index + serializable
        transactions. This is an observability surface, not a chatbot: it exists to
        show what the memory layer did, and why.
      </footer>
    </div>
  );
}
