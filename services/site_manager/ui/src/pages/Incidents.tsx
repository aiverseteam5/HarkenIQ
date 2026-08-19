import { useState } from "react";
import { getJson } from "../api";
import { usePoll } from "../usePoll";
import type { Incident } from "../types";

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
