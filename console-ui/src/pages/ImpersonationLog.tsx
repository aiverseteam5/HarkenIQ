import { type CSSProperties, useCallback, useEffect, useMemo, useState } from "react";
import PageHeader from "../components/PageHeader";
import FilterBar, { type FilterDef } from "../components/FilterBar";
import DataTable, { type Column } from "../components/DataTable";
import DetailPanel from "../components/DetailPanel";
import EmptyState from "../components/EmptyState";
import Spinner from "../components/Spinner";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";
import { getJson } from "../api";

/* ── Types ────────────────────────────────────────── */

interface ImpersonationEntry {
  id: string;
  admin_user_id: string;
  admin_email: string;
  tenant_id: string;
  started_at: string;
  ended_at: string | null;
  actions_count: number;
}

interface AuditTrailEntry {
  id: string;
  ts: string;
  action: string;
  subject_type: string;
  subject_id: string;
}

/* ── Styles ───────────────────────────────────────── */

const detailRow: CSSProperties = {
  display: "flex", justifyContent: "space-between", padding: "0.375rem 0",
  fontSize: "0.8125rem", borderBottom: "1px solid var(--border-light)",
};
const detailLabel: CSSProperties = { color: "var(--text-secondary)", fontWeight: 500 };
const detailValue: CSSProperties = { color: "var(--text-primary)", fontWeight: 500 };
const sectionTitle: CSSProperties = {
  fontSize: "0.75rem", fontWeight: 600, color: "var(--text-secondary)",
  textTransform: "uppercase", marginTop: "1rem", marginBottom: "0.5rem",
};

/* ── Helpers ──────────────────────────────────────── */

function formatDate(iso: string | null): string {
  if (!iso) return "--";
  return new Date(iso).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function todayStr(): string { return new Date().toISOString().slice(0, 10); }
function monthAgoStr(): string { const d = new Date(); d.setMonth(d.getMonth() - 1); return d.toISOString().slice(0, 10); }

/* ── Component ────────────────────────────────────── */

const PAGE_SIZE = 20;

export default function ImpersonationLogPage() {
  const { toasts, toast, dismiss } = useToast();
  const [entries, setEntries] = useState<ImpersonationEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [filters, setFilters] = useState<Record<string, string>>({ admin: "", tenant: "", date_from: monthAgoStr(), date_to: todayStr() });
  const [selected, setSelected] = useState<ImpersonationEntry | null>(null);
  const [auditTrail, setAuditTrail] = useState<AuditTrailEntry[]>([]);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);

  const filterDefs = useMemo<FilterDef[]>(() => [
    { key: "admin", label: "Admin", type: "text", placeholder: "Admin user ID..." },
    { key: "tenant", label: "Tenant", type: "text", placeholder: "Tenant ID..." },
    { key: "date_from", label: "From", type: "date" as const },
    { key: "date_to", label: "To", type: "date" as const },
  ], []);

  const fetchLogs = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (filters.admin) params.set("admin_user_id", filters.admin);
      if (filters.tenant) params.set("tenant_id", filters.tenant);
      if (filters.date_from) params.set("date_from", filters.date_from);
      if (filters.date_to) params.set("date_to", filters.date_to);
      params.set("page", String(page));
      params.set("page_size", String(PAGE_SIZE));
      const res = await getJson<{ items: ImpersonationEntry[]; total: number }>(
        `/api/admin/impersonation?${params.toString()}`,
      );
      setEntries(res.items);
      setTotal(res.total);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load logs", "error");
    } finally {
      setLoading(false);
    }
  }, [filters, page, toast]);

  useEffect(() => { void fetchLogs(); }, [fetchLogs]);

  const openDetail = useCallback(async (entry: ImpersonationEntry) => {
    setDetailOpen(true);
    setDetailLoading(true);
    setSelected(entry);
    try {
      const res = await getJson<{ impersonation: ImpersonationEntry; audit_trail: AuditTrailEntry[] }>(
        `/api/admin/impersonation/${entry.id}`,
      );
      setSelected(res.impersonation);
      setAuditTrail(res.audit_trail);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load detail", "error");
    } finally {
      setDetailLoading(false);
    }
  }, [toast]);

  const columns: Column<ImpersonationEntry>[] = [
    { key: "admin_email", header: "Admin" },
    { key: "tenant_id", header: "Tenant", render: (r) => <code style={{ fontSize: "0.75rem" }}>{r.tenant_id.slice(0, 12)}</code> },
    { key: "started_at", header: "Started", render: (r) => formatDate(r.started_at) },
    { key: "ended_at", header: "Ended", render: (r) => formatDate(r.ended_at) },
    { key: "actions_count", header: "Actions", render: (r) => String(r.actions_count) },
  ];

  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />
      <PageHeader title="Impersonation Log" breadcrumbs={[{ label: "HarkenIQ" }, { label: "Admin" }, { label: "Impersonation" }]} />

      <FilterBar
        filters={filterDefs} values={filters}
        onChange={(k, v) => { setFilters(prev => ({ ...prev, [k]: v })); setPage(1); }}
        onClear={() => { setFilters({ admin: "", tenant: "", date_from: monthAgoStr(), date_to: todayStr() }); setPage(1); }}
      />

      {!loading && entries.length === 0 ? (
        <EmptyState title="No impersonation sessions" description="Impersonation sessions will be logged here when admins access tenant portals." icon="&#x263A;" />
      ) : (
        <DataTable<ImpersonationEntry> columns={columns} data={entries} loading={loading} emptyMessage="No sessions" page={page} pageSize={PAGE_SIZE} total={total} onPageChange={setPage} onRowClick={openDetail} striped />
      )}

      <DetailPanel open={detailOpen} onClose={() => { setDetailOpen(false); setSelected(null); }} title="Impersonation Session" width={520}>
        {detailLoading ? (
          <div style={{ display: "flex", justifyContent: "center", padding: "2rem" }}><Spinner size="md" /></div>
        ) : selected ? (
          <>
            <div style={detailRow}><span style={detailLabel}>Admin</span><span style={detailValue}>{selected.admin_email}</span></div>
            <div style={detailRow}><span style={detailLabel}>Tenant</span><span style={detailValue}><code>{selected.tenant_id}</code></span></div>
            <div style={detailRow}><span style={detailLabel}>Started</span><span style={detailValue}>{formatDate(selected.started_at)}</span></div>
            <div style={detailRow}><span style={detailLabel}>Ended</span><span style={detailValue}>{formatDate(selected.ended_at)}</span></div>
            <div style={detailRow}><span style={detailLabel}>Actions</span><span style={detailValue}>{selected.actions_count}</span></div>

            <div style={sectionTitle}>Audit Trail</div>
            {auditTrail.length === 0 ? (
              <div style={{ fontSize: "0.8125rem", color: "var(--text-muted)" }}>No actions recorded.</div>
            ) : auditTrail.map((e) => (
              <div key={e.id} style={{ ...detailRow, fontSize: "0.75rem" }}>
                <span style={{ color: "var(--text-muted)", minWidth: 100 }}>{formatDate(e.ts)}</span>
                <code>{e.action}</code>
                <span style={{ marginLeft: "auto", color: "var(--text-muted)" }}>{e.subject_type}</span>
              </div>
            ))}
          </>
        ) : null}
      </DetailPanel>
    </div>
  );
}
