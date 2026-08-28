import { useParams } from "react-router-dom";
import { type CSSProperties, useCallback, useEffect, useState } from "react";
import PageHeader from "../components/PageHeader";
import DataTable, { type Column } from "../components/DataTable";
import StatusBadge from "../components/StatusBadge";
import EmptyState from "../components/EmptyState";
import Spinner from "../components/Spinner";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";
import { getJson, postJson } from "../api";

/* ── Types ────────────────────────────────────────── */

interface MarketplaceSkill {
  id: string;
  skill_name: string;
  version: number;
  author_email: string;
  description: string;
  target: string;
  tier: string;             // community | verified | core
  review_status: string;    // submitted | approved | rejected
  published: boolean;
  install_count: number;
  total_executions: number;
  device_count: number;
  success_rate: number | null;
  validation_report?: { warnings?: string[] } | null;
}

/* ── Styles ───────────────────────────────────────── */

const sectionHeader: CSSProperties = {
  fontSize: "0.9375rem", fontWeight: 600, color: "var(--text-primary)",
  marginBottom: "0.75rem", marginTop: "1.5rem",
};

const cardStyle: CSSProperties = {
  background: "var(--bg-card)", borderRadius: "var(--radius-md)",
  border: "1px solid var(--border-light)", padding: "1.25rem", marginBottom: "1.5rem",
};

const textareaStyle: CSSProperties = {
  width: "100%", minHeight: 180, fontFamily: "monospace", fontSize: "0.8125rem",
  background: "var(--bg-primary)", color: "var(--text-primary)",
  border: "1px solid var(--border-light)", borderRadius: "var(--radius-sm)",
  padding: "0.75rem", resize: "vertical",
};

const buttonStyle: CSSProperties = {
  padding: "0.375rem 0.75rem", fontSize: "0.8125rem", fontWeight: 500,
  background: "var(--accent)", color: "#fff", border: "none",
  borderRadius: "var(--radius-sm)", cursor: "pointer",
};

const smallButton: CSSProperties = {
  ...buttonStyle, padding: "0.25rem 0.625rem", fontSize: "0.75rem",
};

const neutralButton: CSSProperties = {
  ...smallButton, background: "var(--bg-card)", color: "var(--text-primary)",
  border: "1px solid var(--border-light)",
};

const TIER_VARIANT: Record<string, "success" | "info" | "neutral"> = {
  core: "success",
  verified: "info",
  community: "neutral",
};

function pct(rate: number | null): string {
  return rate === null ? "--" : `${(rate * 100).toFixed(1)}%`;
}

/* ── Component ────────────────────────────────────── */

export default function SkillMarketplace() {
  const { tenantId = "" } = useParams<{ tenantId: string }>();
  const { toasts, toast, dismiss } = useToast();
  const [loading, setLoading] = useState(true);
  const [published, setPublished] = useState<MarketplaceSkill[]>([]);
  const [queue, setQueue] = useState<MarketplaceSkill[]>([]);
  const [yamlText, setYamlText] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const [pubRes, queueRes] = await Promise.allSettled([
        getJson<{ items: MarketplaceSkill[] }>("/api/marketplace/skills"),
        getJson<{ items: MarketplaceSkill[] }>("/api/admin/marketplace/skills"),
      ]);
      if (pubRes.status === "fulfilled") setPublished(pubRes.value.items);
      if (queueRes.status === "fulfilled") setQueue(queueRes.value.items);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void fetchAll(); }, [fetchAll]);

  const submitSkill = async () => {
    if (!yamlText.trim()) return;
    setSubmitting(true);
    try {
      await postJson("/api/marketplace/skills", { yaml_content: yamlText });
      toast("Skill submitted for review", "success");
      setYamlText("");
      await fetchAll();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Submission failed", "error");
    } finally {
      setSubmitting(false);
    }
  };

  const review = async (id: string, approve: boolean) => {
    try {
      const reason = approve ? "" :
        window.prompt("Rejection reason:") ?? "";
      if (!approve && !reason) return;
      await postJson(`/api/admin/marketplace/skills/${id}/review`,
        { approve, reason });
      toast(approve ? "Skill approved" : "Skill rejected", "success");
      await fetchAll();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Review failed", "error");
    }
  };

  const install = async (id: string) => {
    // Tenant plane: name the tenant the install is FOR. Without it a
    // platform user's install returned 200 and recorded nothing, so CC
    // never delivered the skill [review CRITICAL, api-contract pass].
    try {
      await postJson(
        `/api/marketplace/skills/${id}/install`,
        tenantId ? { tenant_id: tenantId } : {},
      );
      toast("Skill installed — distribute via your Site Manager", "success");
      await fetchAll();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Install failed", "error");
    }
  };

  const promote = async (id: string) => {
    try {
      await postJson(`/api/admin/marketplace/skills/${id}/promote`, {});
      toast("Skill promoted to verified", "success");
      await fetchAll();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Promotion gate not met", "error");
    }
  };

  const publishedColumns: Column<MarketplaceSkill>[] = [
    { key: "skill_name", header: "Skill" },
    { key: "target", header: "Target" },
    { key: "tier", header: "Tier", render: (s) => (
      <StatusBadge status={s.tier} variant={TIER_VARIANT[s.tier] ?? "neutral"} size="sm" />
    )},
    { key: "success_rate", header: "Success", render: (s) => pct(s.success_rate) },
    { key: "total_executions", header: "Runs", render: (s) => String(s.total_executions) },
    { key: "device_count", header: "Devices", render: (s) => String(s.device_count) },
    { key: "install_count", header: "Installs", render: (s) => String(s.install_count) },
    { key: "actions", header: "", render: (s) => (
      <span style={{ display: "flex", gap: "0.375rem" }}>
        <button style={smallButton} onClick={() => void install(s.id)}>Install</button>
        {s.tier === "community" && (
          <button style={neutralButton} onClick={() => void promote(s.id)}>Promote</button>
        )}
      </span>
    )},
  ];

  const queueColumns: Column<MarketplaceSkill>[] = [
    { key: "skill_name", header: "Skill" },
    { key: "target", header: "Target" },
    { key: "author_email", header: "Author" },
    { key: "warnings", header: "Warnings", render: (s) =>
      String(s.validation_report?.warnings?.length ?? 0) },
    { key: "actions", header: "", render: (s) => (
      <span style={{ display: "flex", gap: "0.375rem" }}>
        <button style={smallButton} onClick={() => void review(s.id, true)}>Approve</button>
        <button style={neutralButton} onClick={() => void review(s.id, false)}>Reject</button>
      </span>
    )},
  ];

  if (loading) {
    return (
      <div>
        <PageHeader title="Skill Marketplace" breadcrumbs={[{ label: "HarkenIQ" }, { label: "Operations" }, { label: "Marketplace" }]} />
        <div style={{ display: "flex", justifyContent: "center", padding: "4rem" }}><Spinner size="lg" /></div>
      </div>
    );
  }

  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />
      <PageHeader
        title="Skill Marketplace"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Operations" }, { label: "Marketplace" }]}
      />

      <div style={sectionHeader}>Published Skills</div>
      {published.length === 0 ? (
        <EmptyState title="No published skills yet" description="Approved community skills appear here for installation." icon="&#x2606;" />
      ) : (
        <DataTable<MarketplaceSkill> columns={publishedColumns} data={published} loading={false} emptyMessage="No skills" striped />
      )}

      <div style={sectionHeader}>Review Queue</div>
      {queue.length === 0 ? (
        <EmptyState title="Queue is empty" description="Community submissions awaiting review appear here." icon="&#x2714;" />
      ) : (
        <DataTable<MarketplaceSkill> columns={queueColumns} data={queue} loading={false} emptyMessage="No submissions" striped />
      )}

      <div style={sectionHeader}>Submit a Skill</div>
      <div style={cardStyle}>
        <textarea
          style={textareaStyle}
          value={yamlText}
          onChange={(e) => setYamlText(e.target.value)}
          placeholder={"name: my-skill\nversion: 1\ntarget: fan\nrules:\n  - condition: \"health == 'Warning'\"\n    verdict: WARNING\n    message: \"...\"\ndefault_verdict: HEALTHY"}
        />
        <div style={{ marginTop: "0.75rem" }}>
          <button
            style={{ ...buttonStyle, opacity: submitting ? 0.6 : 1 }}
            disabled={submitting}
            onClick={() => void submitSkill()}
          >
            {submitting ? "Submitting..." : "Submit for Review"}
          </button>
        </div>
      </div>
    </div>
  );
}
