import { useState } from "react";
import { getJson } from "../api";
import { usePoll } from "../usePoll";
import type { Incident, IncidentExplanation } from "../types";

/** QA-009: the LLM explanation is the primary panel of an incident —
 * before this it was computed, stored, served, and rendered nowhere. */
function ExplanationPanel({ explanation }: { explanation: IncidentExplanation }) {
  const [showReasoning, setShowReasoning] = useState(false);
  return (
    <div
      className="card"
      style={{ marginTop: "0.6rem", borderLeft: "3px solid #7c6cf5" }}
    >
      <div>
        <span className="badge kind">DIAGNOSIS</span>
        <span className="muted" style={{ marginLeft: "0.5rem" }}>
          {explanation.provider} · confidence{" "}
          {(explanation.confidence * 100).toFixed(0)}%
        </span>
      </div>
      <div style={{ marginTop: "0.4rem" }}>{explanation.summary}</div>
      {explanation.suggested_action && (
        <div style={{ marginTop: "0.4rem" }}>
          <strong>Suggested action:</strong> {explanation.suggested_action}
        </div>
      )}
      {(explanation.reasoning_steps.length > 0 ||
        explanation.evidence_cited.length > 0) && (
        <button
          className="action"
          style={{ marginTop: "0.4rem" }}
          onClick={() => setShowReasoning(!showReasoning)}
        >
          {showReasoning ? "Hide" : "Show"} reasoning
        </button>
      )}
      {showReasoning && (
        <div style={{ marginTop: "0.4rem" }}>
          {explanation.reasoning_steps.length > 0 && (
            <ol className="muted" style={{ margin: 0, paddingLeft: "1.2rem" }}>
              {explanation.reasoning_steps.map((step, i) => (
                <li key={i}>{step}</li>
              ))}
            </ol>
          )}
          {explanation.evidence_cited.length > 0 && (
            <div className="muted" style={{ marginTop: "0.3rem" }}>
              Evidence cited: {explanation.evidence_cited.join("; ")}
            </div>
          )}
          {explanation.similar_past_incidents.length > 0 && (
            <div className="muted" style={{ marginTop: "0.3rem" }}>
              Similar past incidents:{" "}
              {explanation.similar_past_incidents.join("; ")}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function IncidentCard({ incident }: { incident: Incident }) {
  const [expanded, setExpanded] = useState(false);
  const [showEvidence, setShowEvidence] = useState(false);
  return (
    <div className="card">
      <div>
        <strong>{incident.title || incident.kind}</strong>
        <span className="badge kind">{incident.kind}</span>
        {incident.inferred && (
          <span className="badge inferred">
            INFERRED · {incident.confidence.toFixed(1)}
          </span>
        )}
        {incident.status === "resolved" && (
          <span className="badge resolved">RESOLVED</span>
        )}
      </div>
      <div className="muted">
        {incident.agent_id && <>device: {incident.agent_id} · </>}
        {incident.subsystem && <>subsystem: {incident.subsystem} · </>}
        opened {incident.opened_at}
      </div>
      {incident.explanation && (
        <ExplanationPanel explanation={incident.explanation} />
      )}
      <div style={{ marginTop: "0.5rem" }}>
        {incident.children.length > 0 && (
          <button className="action" onClick={() => setExpanded(!expanded)}>
            {expanded ? "Hide" : "Show"} {incident.children.length} child
            {incident.children.length === 1 ? "" : "ren"}
          </button>
        )}
        {incident.correlation_meta && (
          <button className="action" onClick={() => setShowEvidence(!showEvidence)}>
            {showEvidence ? "Hide" : "Show"} evidence
          </button>
        )}
      </div>
      {showEvidence && incident.correlation_meta && (
        <pre className="evidence">
          {JSON.stringify(incident.correlation_meta, null, 2)}
        </pre>
      )}
      {expanded &&
        incident.children.map((child) => (
          <div key={child.id} className="card" style={{ marginTop: "0.6rem" }}>
            <strong>{child.agent_id ?? child.id}</strong>
            <span className="badge kind">{child.subsystem ?? child.kind}</span>
            {child.status === "resolved" && (
              <span className="badge resolved">RESOLVED</span>
            )}
            <div className="muted">opened {child.opened_at}</div>
            {child.explanation && (
              <ExplanationPanel explanation={child.explanation} />
            )}
            {child.correlation_meta && (
              <pre className="evidence">
                {JSON.stringify(child.correlation_meta, null, 2)}
              </pre>
            )}
          </div>
        ))}
    </div>
  );
}

export default function Incidents({ onAuthFail }: { onAuthFail: () => void }) {
  const [scope, setScope] = useState<"open" | "all">("open");
  const { data, error, authFailed } = usePoll(
    () => getJson<Incident[]>(`/api/incidents?status=${scope}`),
    [scope],
  );
  if (authFailed) onAuthFail();

  return (
    <div>
      {error && <div className="error">{error}</div>}
      <div style={{ marginBottom: "0.8rem" }}>
        <button
          className="action"
          onClick={() => setScope(scope === "open" ? "all" : "open")}
        >
          Showing {scope} — switch to {scope === "open" ? "all" : "open"}
        </button>
      </div>
      {(data ?? []).map((incident) => (
        <IncidentCard key={incident.id} incident={incident} />
      ))}
      {(data ?? []).length === 0 && (
        <span className="muted">No {scope === "open" ? "open " : ""}incidents.</span>
      )}
    </div>
  );
}
