import { useState } from "react";
import { getJson, patchJson } from "../api";
import { usePoll } from "../usePoll";
import type { FaultDomain } from "../types";

function DomainRow({
  domain,
  actor,
  refresh,
  onError,
}: {
  domain: FaultDomain;
  actor: string;
  refresh: () => void;
  onError: (message: string) => void;
}) {
  const [members, setMembers] = useState(domain.members.join(", "));
  const [editing, setEditing] = useState(false);

  const confirm = async () => {
    try {
      await patchJson(`/api/fault-domains/${domain.id}`, { confirm: true, actor });
      refresh();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    }
  };

  const saveMembers = async () => {
    try {
      await patchJson(`/api/fault-domains/${domain.id}`, {
        members: members.split(",").map((m) => m.trim()).filter(Boolean),
        actor,
      });
      setEditing(false);
      refresh();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <tr>
      <td>{domain.name}</td>
      <td>{domain.kind}</td>
      <td>
        {domain.status}
        {domain.status === "inferred" && (
          <span className="badge inferred">
            INFERRED · {domain.confidence.toFixed(1)}
          </span>
        )}
      </td>
      <td>{domain.source}</td>
      <td>
        {editing ? (
          <input
            className="text"
            value={members}
            onChange={(e) => setMembers(e.target.value)}
          />
        ) : (
          domain.members.join(", ") || "—"
        )}
      </td>
      <td>
        {domain.status === "inferred" && (
          <button className="action approve" onClick={() => void confirm()}>
            Confirm
          </button>
        )}
        {editing ? (
          <button className="action" onClick={() => void saveMembers()}>
            Save
          </button>
        ) : (
          <button className="action" onClick={() => setEditing(true)}>
            Edit members
          </button>
        )}
      </td>
    </tr>
  );
}

export default function Domains({
  actor,
  onAuthFail,
}: {
  actor: string;
  onAuthFail: () => void;
}) {
  const [actionError, setActionError] = useState<string | null>(null);
  const { data, error, authFailed, refresh } = usePoll(() =>
    getJson<FaultDomain[]>("/api/fault-domains"),
  );
  if (authFailed) onAuthFail();

  return (
    <div>
      {(error || actionError) && (
        <div className="error">{actionError ?? error}</div>
      )}
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Kind</th>
            <th>Status</th>
            <th>Source</th>
            <th>Members (agent IDs)</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {(data ?? []).map((domain) => (
            <DomainRow
              key={domain.id}
              domain={domain}
              actor={actor}
              refresh={() => void refresh()}
              onError={setActionError}
            />
          ))}
          {(data ?? []).length === 0 && (
            <tr>
              <td colSpan={6} className="muted">
                No fault domains yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
