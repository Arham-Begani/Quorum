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
        <p className="empty" style={{ paddingTop: 80 }}>
          Could not load demo-snapshot.json ({err}). Generate it with{" "}
          <code>python -m quorum.harness.export_demo --rerun</code>.
        </p>
      </div>
    );
  }
  if (!snap) {
    return (
      <div className="wrap">
        <p className="empty" style={{ paddingTop: 80 }}>Reading snapshot…</p>
      </div>
    );
  }

  const offline = snap.providers?.embedder?.provider !== "bedrock_titan";
  const generated = new Date(snap.generated_at);

  /* snap.cluster is the raw version() banner — "CockroachDB CCL v26.2.1
   * (x86_64-pc-linux-gnu, built …, go1.25.5)". The build triple is noise in a
   * 300px rail and wraps to three lines. Keep the part that identifies the
   * cluster; the full string stays in the snapshot for anyone who wants it. */
  const cluster =
    (snap.cluster || "").match(/CockroachDB\s+\S*\s*v[\d.]+/)?.[0]?.replace(/\s+/g, " ") ||
    snap.cluster;

  return (
    <div className="wrap">
      <header className="masthead">
        <div className="rise" style={{ animationDelay: "0ms" }}>
          <div className="wordmark">
            <span className="dot" />
            Quorum
            <span className="sub">memory consistency for multi-agent systems</span>
          </div>

          <h1 className="thesis">
            Transactions solve <span className="strike">write</span> conflicts.
            They do not solve <span className="em">semantic</span> ones.
          </h1>

          <p className="thesis-body">
            Two agents can write mutually contradictory facts as two different
            rows, both commit cleanly under SERIALIZABLE, and the swarm now holds
            memory that is internally inconsistent — and will produce a wrong
            action. Quorum detects contradiction{" "}
            <strong>inside the transaction that commits the write</strong>,
            resolves it under an explicit policy, and refuses to act on contested
            memory.
          </p>
        </div>

        <div className="rail rise" style={{ animationDelay: "90ms" }}>
          <div className="rail-row">
            <span className="rail-k">cluster</span>
            <span className="rail-v" title={snap.cluster}>{cluster}</span>
          </div>
          <div className="rail-row">
            <span className="rail-k">snapshot</span>
            <span className="rail-v">
              {generated.toISOString().slice(0, 16).replace("T", " ")}Z
            </span>
          </div>
          <div className="rail-row">
            <span className="rail-k">embeddings</span>
            <span className="rail-v">{snap.providers?.embedder?.provider}</span>
          </div>
          <div className="rail-row">
            <span className="rail-k">tier-2</span>
            <span className="rail-v">{snap.providers?.tier2?.provider}</span>
          </div>
        </div>

        {offline && (
          <div className="banner rise" style={{ animationDelay: "150ms" }}>
            <span className="glyph">!</span>
            <span>
              This snapshot was produced <strong>without Bedrock</strong>.
              Embeddings come from a deterministic offline stand-in and tier-2
              adjudication fails closed rather than classifying. Everything
              CockroachDB-side — the serializable write path, contradiction
              detection on shared subject keys, supersession, contest, and the
              action gate — is real. Scenarios whose conflicting claims do{" "}
              <em>not</em> share a subject key are marked <strong>NOT TESTED</strong>{" "}
              rather than counted as passing.
            </span>
          </div>
        )}
      </header>

      <div className="controls rise" style={{ animationDelay: "200ms" }}>
        <nav className="tabs">
          {TABS.map(([id, label]) => (
            <button key={id} data-active={tab === id} onClick={() => setTab(id)}>
              {label}
            </button>
          ))}
        </nav>

        <div className="picker">
          <span className="picker-label">scenario</span>
          {snap.scenarios.map((s) => (
            <button
              key={s.id}
              data-active={s.id === scenarioId}
              onClick={() => setScenarioId(s.id)}
              title={s.title}
            >
              {s.id.split("_")[0]}
            </button>
          ))}
        </div>
      </div>

      {scenario && (
        <>
          <div className="scenario-head rise" style={{ animationDelay: "250ms" }}>
            <div>
              <h2>{scenario.title}</h2>
              <div className="tagrow">
                <span className="tag">{scenario.id}</span>
                <span className="tag">
                  {scenario.tier === "tier1" ? "tier 1 · structural" : "tier 2 · semantic"}
                </span>
                {scenario.requires_semantic_embeddings && (
                  <span className="tag amber">needs real embeddings</span>
                )}
              </div>
            </div>
            <p className="desc">{scenario.description}</p>
          </div>

          {tab === "split" && <SplitScreen scenario={scenario} modes={snap.modes} />}
          {tab === "conflicts" && <ConflictLog scenario={scenario} modes={snap.modes} />}
          {tab === "health" && <MemoryHealth snap={snap} scenario={scenario} />}
          {tab === "timeline" && <Timeline scenario={scenario} modes={snap.modes} />}
        </>
      )}

      <footer className="foot">
        <span>Quorum · Apache-2.0 · CockroachDB vector index + serializable transactions</span>
        <span className="note">
          An observability surface, not a chatbot. It exists to show what the
          memory layer did, and why.
        </span>
      </footer>
    </div>
  );
}
