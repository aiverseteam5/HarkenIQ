import { type CSSProperties, useCallback, useEffect, useState } from "react";
import PageHeader from "../components/PageHeader";
import StatusBadge from "../components/StatusBadge";
import ConfirmDialog from "../components/ConfirmDialog";
import Spinner from "../components/Spinner";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";
import { getJson, postJson } from "../api";

/* ── Types ────────────────────────────────────────── */

interface ReleaseInfo {
  current: string;
  latest: string;
  release_notes?: string;
}

/* ── Styles ───────────────────────────────────────── */

const cardStyle: CSSProperties = {
  background: "var(--bg-card)", borderRadius: "var(--radius-md)",
  border: "1px solid var(--border-light)", padding: "1.25rem", marginBottom: "1rem",
};

const releaseGrid: CSSProperties = {
  display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))",
  gap: "1rem", marginBottom: "1.5rem",
};

const releaseCard: CSSProperties = {
  ...cardStyle, marginBottom: 0,
};

const sectionHeader: CSSProperties = {
  fontSize: "0.9375rem", fontWeight: 600, color: "var(--text-primary)",
  marginBottom: "0.75rem", marginTop: "1.5rem",
};

const versionBig: CSSProperties = {
  fontSize: "1.25rem", fontWeight: 700, color: "var(--text-primary)",
  fontFamily: "var(--font-mono, monospace)",
};

const labelSmall: CSSProperties = {
  fontSize: "0.6875rem", fontWeight: 600, color: "var(--text-secondary)",
  textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: "0.25rem",
};

const noteBlock: CSSProperties = {
  marginTop: "0.75rem", padding: "0.75rem", background: "var(--bg-primary)",
  borderRadius: "var(--radius-sm)", fontSize: "0.8125rem", color: "var(--text-secondary)",
  whiteSpace: "pre-wrap",
};

const COMPONENTS = ["site_manager", "agent", "cli", "skill_packs"];
const COMPONENT_LABELS: Record<string, string> = {
  site_manager: "Site Manager",
  agent: "Agent",
  cli: "CLI",
  skill_packs: "Skill Packs",
};

/* ── Component ────────────────────────────────────── */

export default function ReleaseManagement() {
  const { toasts, toast, dismiss } = useToast();
  const [loading, setLoading] = useState(true);
  const [releases, setReleases] = useState<Record<string, ReleaseInfo>>({});
  const [showUpdate, setShowUpdate] = useState(false);
  const [updateForm, setUpdateForm] = useState({ component: "agent", version: "", release_notes: "" });
  const [updating, setUpdating] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);

  const fetchReleases = useCallback(async () => {
    setLoading(true);
    try {
      const res = await getJson<{ releases: Record<string, ReleaseInfo> }>("/api/admin/releases");
      setReleases(res.releases);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load releases", "error");
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => { void fetchReleases(); }, [fetchReleases]);

  const handleUpdate = useCallback(async () => {
    if (!updateForm.version.trim()) return;
    setUpdating(true);
    try {
      await postJson("/api/admin/releases", updateForm);
      toast(`${COMPONENT_LABELS[updateForm.component]} updated to ${updateForm.version}`, "success");
      setShowUpdate(false);
      setUpdateForm({ component: "agent", version: "", release_notes: "" });
      void fetchReleases();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Update failed", "error");
    } finally {
      setUpdating(false);
    }
  }, [updateForm, toast, fetchReleases]);

  if (loading) return (
    <div>
      <PageHeader title="Releases" breadcrumbs={[{ label: "HarkenIQ" }, { label: "Admin" }, { label: "Releases" }]} />
      <div style={{ display: "flex", justifyContent: "center", padding: "4rem" }}><Spinner size="lg" /></div>
    </div>
  );

  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />
      <PageHeader
        title="Release Management"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Admin" }, { label: "Releases" }]}
        actions={[{ label: "Update Release", onClick: () => setShowUpdate(true), variant: "primary" as const }]}
      />

      <div style={sectionHeader}>Current Versions</div>
      <div style={releaseGrid}>
        {COMPONENTS.map(comp => {
          const info = releases[comp];
          const isUpToDate = info && info.current === info.latest;
          return (
            <div key={comp} style={releaseCard}>
              <div style={labelSmall}>{COMPONENT_LABELS[comp] ?? comp}</div>
              <div style={versionBig}>{info?.current ?? "N/A"}</div>
              <div style={{ marginTop: "0.375rem" }}>
                <StatusBadge
                  status={isUpToDate ? "up to date" : `${info?.latest ?? "?"} available`}
                  variant={isUpToDate ? "success" : "warning"}
                  size="sm"
                />
              </div>
              {info?.release_notes && (
                <button
                  className="btn btn-sm"
                  style={{ marginTop: "0.5rem", fontSize: "0.75rem" }}
                  onClick={() => setExpanded(expanded === comp ? null : comp)}
                >
                  {expanded === comp ? "Hide notes" : "Release notes"}
                </button>
              )}
              {expanded === comp && info?.release_notes && (
                <div style={noteBlock}>{info.release_notes}</div>
              )}
            </div>
          );
        })}
      </div>

      {showUpdate && (
        <ConfirmDialog title="Update Release" onConfirm={handleUpdate} onCancel={() => setShowUpdate(false)} confirmLabel={updating ? "Updating..." : "Update"}>
          <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
            <select value={updateForm.component} onChange={(e) => setUpdateForm(f => ({ ...f, component: e.target.value }))} style={{ padding: "0.5rem", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-light)" }}>
              {COMPONENTS.map(c => <option key={c} value={c}>{COMPONENT_LABELS[c]}</option>)}
            </select>
            <input placeholder="Version (e.g. 0.2.0)" value={updateForm.version} onChange={(e) => setUpdateForm(f => ({ ...f, version: e.target.value }))} style={{ padding: "0.5rem", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-light)" }} />
            <textarea placeholder="Release notes" value={updateForm.release_notes} onChange={(e) => setUpdateForm(f => ({ ...f, release_notes: e.target.value }))} rows={4} style={{ padding: "0.5rem", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-light)", resize: "vertical" }} />
          </div>
        </ConfirmDialog>
      )}
    </div>
  );
}
