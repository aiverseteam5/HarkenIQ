import { type CSSProperties, useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import FilterBar, { type FilterDef } from "../components/FilterBar";
import DataTable, { type Column } from "../components/DataTable";
import DetailPanel from "../components/DetailPanel";
import EmptyState from "../components/EmptyState";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";
import { getJson } from "../api";

/* ── Types ────────────────────────────────────────── */

interface AuditEntry {
  id: string;
  ts: string;
  actor_id: string | null;
  actor_email: string;
  action: string;
  subject_type: string;
  subject_id: string;
  tenant_id: string | null;
  detail: Record<string, unknown> | null;
}

interface ChainVerification {
  valid: boolean;
  length: number;
  first_bad_seq: number | null;
  error: string | null;
}

/* ── Styles ───────────────────────────────────────── */

const detailRow: CSSProperties = {
  display: "flex", justifyContent: "space-between", padding: "0.375rem 0",
  fontSize: "0.8125rem", borderBottom: "1px solid var(--border-light)",
};
const detailLabel: CSSProperties = { color: "var(--text-secondary)", fontWeight: 500 };
const detailValue: CSSProperties = { color: "var(--text-primary)", fontWeight: 500, textAlign: "right", maxWidth: "60%", wordBreak: "break-all" };
const jsonBlock: CSSProperties = {
  background: "var(--bg-primary)", borderRadius: "var(--radius-sm)", padding: "0.75rem",
  fontSize: "0.75rem", fontFamily: "var(--font-mono, monospace)", whiteSpace: "pre-wrap",
  maxHeight: 300, overflow: "auto", marginTop: "0.75rem",
};

function chainBanner(valid: boolean | null): CSSProperties {
  const tone =
    valid === true ? "var(--status-ok, #2E7D32)"
      : valid === false ? "var(--status-critical, #B3372E)"
        : "var(--border-light)";
  return {
    display: "flex", alignItems: "center", gap: "0.75rem", flexWrap: "wrap",
    padding: "0.625rem 0.875rem", marginBottom: "1rem",
    background: "var(--bg-card)", border: "1px solid var(--border-light)",
    borderLeft: `3px solid ${tone}`, borderRadius: "var(--radius-md)",
    fontSize: "0.8125rem",
  };
}

/* ── Helpers ──────────────────────────────────────── */

function formatDate(iso: string | null): string {
  if (!iso) return "--";
  return new Date(iso).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function todayStr(): string { return new Date().toISOString().slice(0, 10); }
function monthAgoStr(): string {
  const d = new Date(); d.setMonth(d.getMonth() - 1);
  return d.toISOString().slice(0, 10);
}

/* ── Component ────────────────────────────────────── */

const PAGE_SIZE = 50;

export default function AuditLogs() {
  const { toasts, toast, dismiss } = useToast();
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [filters, setFilters] = useState<Record<string, string>>({
    actor: "", action: "", date_from: monthAgoStr(), date_to: todayStr(), search: "",
  });
  const [selected, setSelected] = useState<AuditEntry | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  /* S1 2026-08-29: the chain-verify endpoints existed at every service with
     no UI consumer. Proof is a product feature (PRD principle 6) — say it
     on the page instead of leaving it to curl. */
  const [chain, setChain] = useState<ChainVerification | null>(null);
  const [chainState, setChainState] = useState<"loading" | "ok" | "unavailable">("loading");

  const { tenantId = "" } = useParams<{ tenantId: string }>();

  const filterDefs = useMemo<FilterDef[]>(() => [
    { key: "actor", label: "Actor", type: "text", placeholder: "Email..." },
    { key: "action", label: "Action", type: "text", placeholder: "e.g. ticket.created" },
    { key: "date_from", label: "From", type: "date" as const },
    { key: "date_to", label: "To", type: "date" as const },
    { key: "search", label: "Search", type: "text", placeholder: "Search..." },
  ], []);

  const fetchLogs = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (filters.actor) params.set("actor", filters.actor);
      if (filters.action) params.set("action", filters.action);
      if (filters.date_from) params.set("date_from", filters.date_from);
      if (filters.date_to) params.set("date_to", filters.date_to);
      if (filters.search) params.set("search", filters.search);
      params.set("page", String(page));
      params.set("page_size", String(PAGE_SIZE));
      const res = await getJson<{ items: AuditEntry[]; total: number }>(
        `/api/tenants/${tenantId}/audit?${params.toString()}`,
      );
      setEntries(res.items);
      setTotal(res.total);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load audit logs", "error");
    } finally {
      setLoading(false);
    }
  }, [filters, page, tenantId, toast]);

  useEffect(() => { void fetchLogs(); }, [fetchLogs]);

  const verifyChain = useCallback(async () => {
    setChainState("loading");
    try {
      // The TENANT's own operational chain, at Central Command, through the
      // placement-resolved proxy. (The Console's platform chain has its own
      // super-admin verify endpoint and is not this page's subject.)
      const res = await getJson<ChainVerification>(`/api/t/${tenantId}/audit/verify`);
      setChain(res);
      setChainState("ok");
    } catch {
      setChain(null);
      setChainState("unavailable");
    }
  }, [tenantId]);

  useEffect(() => { void verifyChain(); }, [verifyChain]);

  const handleExport = useCallback((format: "csv" | "json") => {
    const params = new URLSearchParams();
    params.set("format", format);
    if (filters.date_from) params.set("date_from", filters.date_from);
    if (filters.date_to) params.set("date_to", filters.date_to);
    window.open(`/api/tenants/${tenantId}/audit/export?${params.toString()}`, "_blank");
  }, [tenantId, filters]);

  const columns: Column<AuditEntry>[] = [
    { key: "ts", header: "Time", sortKey: "ts", render: (r) => formatDate(r.ts) },
    { key: "actor_email", header: "Actor" },
    { key: "action", header: "Action", render: (r) => <code style={{ fontSize: "0.8125rem" }}>{r.action}</code> },
    { key: "subject_type", header: "Subject" },
    { key: "subject_id", header: "Subject ID", render: (r) => <code style={{ fontSize: "0.75rem" }}>{r.subject_id.slice(0, 12)}</code> },
  ];

  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />
      <PageHeader
        title="Audit Logs"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Operations" }, { label: "Audit Logs" }]}
        actions={[
          { label: "Export CSV", onClick: () => handleExport("csv"), variant: "default" as const },
          { label: "Export JSON", onClick: () => handleExport("json"), variant: "default" as const },
        ]}
      />

      {/* Chain integrity: the tenant's Central Command audit chain. A red
          state here is a compliance event, not a cosmetic one. */}
      <div style={chainBanner(chainState === "ok" && chain ? chain.valid : null)}>
        {/* Name the chain. The table below lists the Console's governance
            events; this verifies the OPERATIONAL chain at Central Command.
            Claiming otherwise would be the exact class of untruth P0
            removed from this product. */}
        <span style={{ fontWeight: 600 }}>
          {chainState === "loading" && "Verifying operational audit chain..."}
          {chainState === "unavailable" && "Operational chain: verification unavailable"}
          {chainState === "ok" && chain?.valid && "Operational audit chain verified"}
          {chainState === "ok" && chain && !chain.valid && "OPERATIONAL AUDIT CHAIN BROKEN"}
        </span>
        <span style={{ color: "var(--text-secondary)" }}>
          {chainState === "ok" && chain?.valid &&
            `Central Command · ${chain.length} entries, cryptographically linked`}
          {chainState === "ok" && chain && !chain.valid &&
            `Central Command · first bad entry #${chain.first_bad_seq ?? "?"}${chain.error ? ` — ${chain.error}` : ""}`}
          {chainState === "unavailable" &&
            "Central Command did not answer. The governance entries below are still readable."}
        </span>
        <button
          className="btn btn-sm"
          style={{ marginLeft: "auto" }}
          onClick={() => void verifyChain()}
          disabled={chainState === "loading"}
        >
          Re-verify
        </button>
      </div>

      <FilterBar
        filters={filterDefs}
        values={filters}
        onChange={(k, v) => { setFilters(prev => ({ ...prev, [k]: v })); setPage(1); }}
        onClear={() => { setFilters({ actor: "", action: "", date_from: monthAgoStr(), date_to: todayStr(), search: "" }); setPage(1); }}
      />

      {!loading && entries.length === 0 ? (
        <EmptyState title="No audit entries" description="Audit log entries will appear here as actions are performed." icon="&#x2630;" />
      ) : (
        <DataTable<AuditEntry>
          columns={columns} data={entries} loading={loading}
          emptyMessage="No entries match your filters" page={page}
          pageSize={PAGE_SIZE} total={total} onPageChange={setPage}
          onRowClick={(e) => { setSelected(e); setDetailOpen(true); }} striped
        />
      )}

      <DetailPanel
        open={detailOpen}
        onClose={() => { setDetailOpen(false); setSelected(null); }}
        title="Audit Entry"
        subtitle={selected?.action}
        width={480}
      >
        {selected && (
          <>
            <div style={detailRow}><span style={detailLabel}>Time</span><span style={detailValue}>{formatDate(selected.ts)}</span></div>
            <div style={detailRow}><span style={detailLabel}>Actor</span><span style={detailValue}>{selected.actor_email || selected.actor_id || "system"}</span></div>
            <div style={detailRow}><span style={detailLabel}>Action</span><span style={detailValue}><code>{selected.action}</code></span></div>
            <div style={detailRow}><span style={detailLabel}>Subject Type</span><span style={detailValue}>{selected.subject_type || "--"}</span></div>
            <div style={detailRow}><span style={detailLabel}>Subject ID</span><span style={detailValue}><code>{selected.subject_id || "--"}</code></span></div>
            <div style={detailRow}><span style={detailLabel}>Tenant</span><span style={detailValue}>{selected.tenant_id || "platform"}</span></div>
            {selected.detail && Object.keys(selected.detail).length > 0 && (
              <>
                <div style={{ fontSize: "0.75rem", fontWeight: 600, color: "var(--text-secondary)", marginTop: "1rem", textTransform: "uppercase" }}>Detail</div>
                <div style={jsonBlock}>{JSON.stringify(selected.detail, null, 2)}</div>
              </>
            )}
          </>
        )}
      </DetailPanel>
    </div>
  );
}
