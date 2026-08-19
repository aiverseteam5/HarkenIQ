import { getJson } from "../api";
import { usePoll } from "../usePoll";
import type { CoverageEntry, Incident } from "../types";

function tileClass(entry: CoverageEntry): string {
  if (entry.observation !== "observed") return "grey";
  if (entry.health === "ok") return "green";
  return "amber";
}

export default function Overview({ onAuthFail }: { onAuthFail: () => void }) {
  const coverage = usePoll(() => getJson<CoverageEntry[]>("/api/coverage"));
  const incidents = usePoll(() => getJson<Incident[]>("/api/incidents"));
  if (coverage.authFailed || incidents.authFailed) onAuthFail();

  const entries = coverage.data ?? [];
  const open = incidents.data ?? [];
  return (
    <div>
      {coverage.error && <div className="error">{coverage.error}</div>}
      <div className="card">
        <span className="stat">{open.length}</span>
        <span className="muted"> open incident{open.length === 1 ? "" : "s"}</span>
        <span style={{ marginLeft: "2rem" }} className="stat">
          {entries.length}
        </span>
        <span className="muted"> device{entries.length === 1 ? "" : "s"}</span>
      </div>
      <h2>Coverage</h2>
      <div className="grid">
        {entries.map((e) => (
          <div key={e.device_id} className={`tile ${tileClass(e)}`}>
            <div className="name">{e.agent_name || e.agent_id}</div>
            <div className="sub">
              {e.observation === "observed"
                ? `health: ${e.health}`
                : e.observation /* silent device is never shown healthy */}
            </div>
          </div>
        ))}
        {entries.length === 0 && <span className="muted">No devices yet.</span>}
      </div>
    </div>
  );
}
