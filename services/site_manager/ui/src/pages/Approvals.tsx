import { useState } from "react";
import { getJson, postJson } from "../api";
import { usePoll } from "../usePoll";
import type { Action } from "../types";

export default function Approvals({
  actor,
  onAuthFail,
}: {
  actor: string;
  onAuthFail: () => void;
}) {
  const [actionError, setActionError] = useState<string | null>(null);
  const { data, error, authFailed, refresh } = usePoll(() =>
    getJson<Action[]>("/api/actions"),
  );
  if (authFailed) onAuthFail();

  const decide = async (id: string, decision: "approve" | "deny") => {
    try {
      setActionError(null);
      await postJson(`/api/actions/${id}/${decision}`, { actor });
      await refresh();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
  };

  const actions = data ?? [];
  const pending = actions.filter((a) => a.status === "pending");
  const others = actions.filter((a) => a.status !== "pending");

  return (
    <div>
      {(error || actionError) && (
        <div className="error">{actionError ?? error}</div>
      )}
      <h2>Pending approval</h2>
      {pending.map((a) => (
        <div key={a.id} className="card">
          <strong>{a.type}</strong>
          <span className="badge kind">{a.sensor_id}</span>
          <span className="badge critical">{a.verdict_severity}</span>
          <div className="muted">
            device: {a.agent_id ?? "?"} · skill: {a.skill_name} · proposed{" "}
            {a.proposed_at}
          </div>
          {a.params && (
            <pre className="evidence">{JSON.stringify(a.params, null, 2)}</pre>
          )}
          <div>
            <button
              className="action approve"
              disabled={!actor.trim()}
              title={
                actor.trim()
                  ? `Recorded as sm-local:${actor}`
                  : "Enter your name in the header first (QA-006)"
              }
              onClick={() => void decide(a.id, "approve")}
            >
              {actor.trim() ? `Approve as ${actor}` : "Enter name to approve"}
            </button>
            <button
              className="action deny"
              disabled={!actor.trim()}
              onClick={() => void decide(a.id, "deny")}
            >
              Deny
            </button>
          </div>
        </div>
      ))}
      {pending.length === 0 && <span className="muted">Nothing waiting.</span>}

      <h2>History</h2>
      <table>
        <thead>
          <tr>
            <th>Type</th>
            <th>Device</th>
            <th>Sensor</th>
            <th>Status</th>
            <th>Decided by</th>
            <th>Outcome</th>
          </tr>
        </thead>
        <tbody>
          {others.map((a) => (
            <tr key={a.id}>
              <td>{a.type}</td>
              <td>{a.agent_id ?? "?"}</td>
              <td>{a.sensor_id}</td>
              <td>{a.status}</td>
              <td>{a.decided_by ?? "—"}</td>
              <td>{a.outcome ? JSON.stringify(a.outcome) : "—"}</td>
            </tr>
          ))}
          {others.length === 0 && (
            <tr>
              <td colSpan={6} className="muted">
                No decided actions yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
