import { type CSSProperties, useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import StatusBadge from "../components/StatusBadge";
import Spinner from "../components/Spinner";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";
import { getJson } from "../api";

/* ── Types ────────────────────────────────────────── */

interface ReleaseInfo { current: string; latest: string; release_notes?: string; }

/* ── Styles ───────────────────────────────────────── */

const cardStyle: CSSProperties = {
  background: "var(--bg-card)", borderRadius: "var(--radius-md)",
  border: "1px solid var(--border-light)", padding: "1.25rem", marginBottom: "1rem",
};
const releaseGrid: CSSProperties = {
  display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
  gap: "1rem", marginBottom: "1.5rem",
};
const versionBig: CSSProperties = {
  fontSize: "1.25rem", fontWeight: 700, fontFamily: "var(--font-mono, monospace)",
};
const labelSmall: CSSProperties = {
  fontSize: "0.6875rem", fontWeight: 600, color: "var(--text-secondary)",
  textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: "0.25rem",
};
const noteBlock: CSSProperties = {
  marginTop: "0.75rem", padding: "0.75rem", background: "var(--bg-primary)",
  borderRadius: "var(--radius-sm)", fontSize: "0.8125rem", whiteSpace: "pre-wrap",
};

const COMPONENTS: Record<string, { label: string; description: string }> = {
  site_manager: { label: "Site Manager", description: "Per-site multi-device correlation and command brokering" },
  agent: { label: "Harken Agent", description: "Per-device embedded agent for hardware diagnostics" },
  cli: { label: "CLI", description: "Command-line interface for agent and site operations" },
  skill_packs: { label: "Skill Packs", description: "Diagnostic and remediation skill modules" },
};

/* ── Component ────────────────────────────────────── */

export default function Downloads() {
  const { tenantId = "" } = useParams<{ tenantId: string }>();
  const { toasts, toast, dismiss } = useToast();
  const [loading, setLoading] = useState(true);
  const [releases, setReleases] = useState<Record<string, ReleaseInfo>>({});
  const [expanded, setExpanded] = useState<string | null>(null);

  const fetchReleases = useCallback(async () => {
    setLoading(true);
    try {
      // P0 2026-08-29: this page fetched /api/admin/releases
      // (super-admin only) behind a site.view nav rule — every tenant
      // role 403ed. It reads the tenant-scoped endpoint now.
      const res = await getJson<{ releases: Record<string, ReleaseInfo> }>(
        `/api/tenants/${tenantId}/releases`,
      );
      setReleases(res.releases);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load", "error");
    } finally {
      setLoading(false);
    }
  }, [toast, tenantId]);

  useEffect(() => { void fetchReleases(); }, [fetchReleases]);

  if (loading) return (
    <div>
      <PageHeader title="Downloads & Upgrades" breadcrumbs={[{ label: "HarkenIQ" }, { label: "Downloads" }]} />
      <div style={{ display: "flex", justifyContent: "center", padding: "4rem" }}><Spinner size="lg" /></div>
    </div>
  );

  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />
      <PageHeader title="Downloads & Upgrades" breadcrumbs={[{ label: "HarkenIQ" }, { label: "Downloads" }]} />

      <div style={releaseGrid}>
        {Object.entries(COMPONENTS).map(([key, meta]) => {
          const info = releases[key];
          const upToDate = info && info.current === info.latest;
          return (
            <div key={key} style={cardStyle}>
              <div style={labelSmall}>{meta.label}</div>
              <div style={versionBig}>{info?.current ?? "N/A"}</div>
              <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", margin: "0.25rem 0 0.5rem" }}>{meta.description}</div>
              <StatusBadge status={upToDate ? "up to date" : `${info?.latest ?? "?"} available`} variant={upToDate ? "success" : "warning"} size="sm" />
              {/* P0 2026-08-29: the Download/Upgrade buttons did nothing
                  (no artifact store exists yet) — removed rather than
                  shipped as dead controls. Version + notes are real. */}
              <div style={{ display: "flex", gap: "0.5rem", marginTop: "0.75rem" }}>
                {info?.release_notes && (
                  <button className="btn btn-sm" onClick={() => setExpanded(expanded === key ? null : key)}>
                    {expanded === key ? "Hide" : "Notes"}
                  </button>
                )}
              </div>
              {expanded === key && info?.release_notes && <div style={noteBlock}>{info.release_notes}</div>}
            </div>
          );
        })}
      </div>
    </div>
  );
}
