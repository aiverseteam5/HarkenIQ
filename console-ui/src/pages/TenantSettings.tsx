import { type CSSProperties, useCallback, useState } from "react";
import PageHeader from "../components/PageHeader";
import StatusBadge from "../components/StatusBadge";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";

/* ── Styles ───────────────────────────────────────── */

const cardStyle: CSSProperties = {
  background: "var(--bg-card)", borderRadius: "var(--radius-md)",
  border: "1px solid var(--border-light)", padding: "1.25rem", marginBottom: "1.5rem",
};
const cardTitle: CSSProperties = {
  fontSize: "0.8125rem", fontWeight: 600, color: "var(--text-secondary)",
  textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: "0.75rem",
};
const fieldRow: CSSProperties = {
  display: "flex", justifyContent: "space-between", alignItems: "center",
  padding: "0.5rem 0", borderBottom: "1px solid var(--border-light)",
  fontSize: "0.8125rem",
};
const label: CSSProperties = { color: "var(--text-secondary)", fontWeight: 500 };
const inputStyle: CSSProperties = {
  padding: "0.375rem 0.5rem", borderRadius: "var(--radius-sm)",
  border: "1px solid var(--border-light)", fontSize: "0.8125rem", width: 220,
};
const toggleStyle: CSSProperties = {
  width: 36, height: 20, borderRadius: 10, cursor: "pointer",
  border: "none", position: "relative", transition: "background 0.2s",
};

/* ── Component ────────────────────────────────────── */

export default function TenantSettings() {
  const { toasts, toast, dismiss } = useToast();
  const [notifications, setNotifications] = useState("daily_digest");
  const [airGapped, setAirGapped] = useState(false);
  const [primaryColor, setPrimaryColor] = useState("#6366f1");
  const [slackWebhook, setSlackWebhook] = useState("");
  const [saving, setSaving] = useState(false);

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      // In production, POST to /api/tenants/{id}/settings
      await new Promise(r => setTimeout(r, 300));
      toast("Settings saved", "success");
    } catch (err) {
      toast(err instanceof Error ? err.message : "Save failed", "error");
    } finally {
      setSaving(false);
    }
  }, [toast]);

  const Toggle = ({ on, onClick }: { on: boolean; onClick: () => void }) => (
    <button style={{ ...toggleStyle, background: on ? "var(--accent)" : "var(--border-light)" }} onClick={onClick}>
      <span style={{ position: "absolute", top: 2, left: on ? 18 : 2, width: 16, height: 16, borderRadius: "50%", background: "#fff", transition: "left 0.2s" }} />
    </button>
  );

  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />
      <PageHeader
        title="Tenant Settings"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Settings" }]}
        actions={[{ label: saving ? "Saving..." : "Save", onClick: handleSave, variant: "primary" as const }]}
      />

      {/* Notifications */}
      <div style={cardStyle}>
        <div style={cardTitle}>Notifications</div>
        <div style={fieldRow}>
          <span style={label}>Email frequency</span>
          <select value={notifications} onChange={(e) => setNotifications(e.target.value)} style={inputStyle}>
            <option value="daily_digest">Daily digest</option>
            <option value="alerts_only">Alerts only</option>
            <option value="per_event">Per event</option>
            <option value="off">Off</option>
          </select>
        </div>
        <div style={fieldRow}>
          <span style={label}>Slack webhook URL</span>
          <input value={slackWebhook} onChange={(e) => setSlackWebhook(e.target.value)} placeholder="https://hooks.slack.com/..." style={inputStyle} />
        </div>
        <div style={fieldRow}>
          <span style={label}>Teams integration</span>
          <StatusBadge status="not configured" variant="neutral" size="sm" />
        </div>
      </div>

      {/* Deployment */}
      <div style={cardStyle}>
        <div style={cardTitle}>Deployment</div>
        <div style={fieldRow}>
          <span style={label}>Air-gapped mode</span>
          <Toggle on={airGapped} onClick={() => setAirGapped(!airGapped)} />
        </div>
        <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", padding: "0.25rem 0" }}>
          Air-gapped mode uses signed usage reports instead of live telemetry sync.
        </div>
      </div>

      {/* Branding */}
      <div style={cardStyle}>
        <div style={cardTitle}>Branding</div>
        <div style={fieldRow}>
          <span style={label}>Primary color</span>
          <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
            <input type="color" value={primaryColor} onChange={(e) => setPrimaryColor(e.target.value)} style={{ width: 32, height: 32, border: "none", cursor: "pointer" }} />
            <code style={{ fontSize: "0.75rem" }}>{primaryColor}</code>
          </div>
        </div>
        <div style={fieldRow}>
          <span style={label}>Custom logo</span>
          <button className="btn btn-sm">Upload</button>
        </div>
      </div>

      {/* SSO */}
      <div style={cardStyle}>
        <div style={cardTitle}>SSO / Identity Federation</div>
        <div style={fieldRow}>
          <span style={label}>SAML/OIDC provider</span>
          <StatusBadge status="not configured" variant="neutral" size="sm" />
        </div>
        <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", padding: "0.25rem 0" }}>
          Configure Keycloak identity federation with your organization's IdP.
        </div>
      </div>
    </div>
  );
}
