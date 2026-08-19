import { getJson } from "../api";
import { usePoll } from "../usePoll";
import type { Device } from "../types";

export default function Devices({ onAuthFail }: { onAuthFail: () => void }) {
  const { data, error, authFailed } = usePoll(() =>
    getJson<Device[]>("/api/devices"),
  );
  if (authFailed) onAuthFail();

  return (
    <div>
      {error && <div className="error">{error}</div>}
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Agent ID</th>
            <th>Vendor</th>
            <th>Model</th>
            <th>Service tag</th>
            <th>Rack</th>
            <th>Observation</th>
            <th>Health</th>
            <th>Last heartbeat</th>
          </tr>
        </thead>
        <tbody>
          {(data ?? []).map((d) => (
            <tr key={d.device_id}>
              <td>{d.agent_name || "—"}</td>
              <td>{d.agent_id}</td>
              <td>{d.vendor || "—"}</td>
              <td>{d.model || "—"}</td>
              <td>{d.service_tag || "—"}</td>
              <td>{d.rack_suggestion || "—"}</td>
              <td>{d.observation}</td>
              <td>{d.health}</td>
              <td>{d.last_heartbeat_at ?? "never"}</td>
            </tr>
          ))}
          {(data ?? []).length === 0 && (
            <tr>
              <td colSpan={9} className="muted">
                No devices registered.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
