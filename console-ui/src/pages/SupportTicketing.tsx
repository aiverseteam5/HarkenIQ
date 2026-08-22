import { type CSSProperties, useCallback, useEffect, useMemo, useState } from "react";
import PageHeader from "../components/PageHeader";
import FilterBar, { type FilterDef } from "../components/FilterBar";
import DataTable, { type Column } from "../components/DataTable";
import DetailPanel from "../components/DetailPanel";
import StatusBadge from "../components/StatusBadge";
import EmptyState from "../components/EmptyState";
import ConfirmDialog from "../components/ConfirmDialog";
import Spinner from "../components/Spinner";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";
import { getJson, postJson, patchJson } from "../api";

/* ── Types ────────────────────────────────────────── */

interface Ticket {
  id: string;
  tenant_id: string;
  ticket_number: number;
  subject: string;
  body: string;
  severity: string;
  component: string;
  site_name: string | null;
  status: string;
  assigned_to: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  closed_at: string | null;
  sla_due_at: string | null;
}

interface Message {
  id: string;
  author_id: string;
  author_email: string;
  body: string;
  is_internal: boolean;
  created_at: string;
}

/* ── Styles ───────────────────────────────────────── */

const cardStyle: CSSProperties = {
  background: "var(--bg-card)", borderRadius: "var(--radius-md)",
  border: "1px solid var(--border-light)", padding: "1.25rem", marginBottom: "1rem",
};

const msgBubble: CSSProperties = {
  padding: "0.75rem", borderRadius: "var(--radius-md)", marginBottom: "0.5rem",
  fontSize: "0.8125rem", background: "var(--bg-primary)",
};

const internalBubble: CSSProperties = {
  ...msgBubble, background: "#fef3c7", border: "1px solid #f59e0b",
};

const replyBox: CSSProperties = {
  display: "flex", flexDirection: "column", gap: "0.5rem", marginTop: "1rem",
};

const SEVERITY_VARIANT: Record<string, "critical" | "warning" | "info" | "neutral"> = {
  S1: "critical", S2: "warning", S3: "info", S4: "neutral",
};

const STATUS_VARIANT: Record<string, "success" | "warning" | "info" | "neutral" | "critical"> = {
  open: "info", acknowledged: "warning", in_progress: "warning",
  waiting_on_tenant: "neutral", closed: "success",
};

/* ── Helpers ──────────────────────────────────────── */

function formatDate(iso: string | null): string {
  if (!iso) return "--";
  return new Date(iso).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function isSlaOverdue(sla: string | null, status: string): boolean {
  if (!sla || status === "closed") return false;
  return new Date(sla) < new Date();
}

/* ── Component ────────────────────────────────────── */

const PAGE_SIZE = 20;

export default function SupportTicketing() {
  const { toasts, toast, dismiss } = useToast();
  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [filters, setFilters] = useState<Record<string, string>>({ status: "", severity: "", search: "" });
  const [selectedTicket, setSelectedTicket] = useState<Ticket | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [replyText, setReplyText] = useState("");
  const [replying, setReplying] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [createForm, setCreateForm] = useState({ subject: "", body: "", severity: "S3", component: "Other" });
  const [creating, setCreating] = useState(false);
  const [closeConfirm, setCloseConfirm] = useState<string | null>(null);

  const tenantId = "current";

  const filterDefs = useMemo<FilterDef[]>(() => [
    { key: "status", label: "Status", type: "select", options: [
      { value: "open", label: "Open" }, { value: "acknowledged", label: "Acknowledged" },
      { value: "in_progress", label: "In Progress" }, { value: "waiting_on_tenant", label: "Waiting" },
      { value: "closed", label: "Closed" },
    ]},
    { key: "severity", label: "Severity", type: "select", options: [
      { value: "S1", label: "S1 - Critical" }, { value: "S2", label: "S2 - High" },
      { value: "S3", label: "S3 - Medium" }, { value: "S4", label: "S4 - Low" },
    ]},
    { key: "search", label: "Search", type: "text", placeholder: "Search tickets..." },
  ], []);

  const fetchTickets = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (filters.status) params.set("status", filters.status);
      if (filters.severity) params.set("severity", filters.severity);
      if (filters.search) params.set("search", filters.search);
      params.set("page", String(page));
      params.set("page_size", String(PAGE_SIZE));
      const res = await getJson<{ items: Ticket[]; total: number }>(
        `/api/tenants/${tenantId}/tickets?${params.toString()}`,
      );
      setTickets(res.items);
      setTotal(res.total);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load tickets", "error");
    } finally {
      setLoading(false);
    }
  }, [filters, page, tenantId, toast]);

  useEffect(() => { void fetchTickets(); }, [fetchTickets]);

  const openDetail = useCallback(async (ticket: Ticket) => {
    setDetailOpen(true);
    setDetailLoading(true);
    setReplyText("");
    try {
      const res = await getJson<{ ticket: Ticket; messages: Message[] }>(
        `/api/tenants/${tenantId}/tickets/${ticket.id}`,
      );
      setSelectedTicket(res.ticket);
      setMessages(res.messages);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load ticket", "error");
      setDetailOpen(false);
    } finally {
      setDetailLoading(false);
    }
  }, [tenantId, toast]);

  const handleReply = useCallback(async () => {
    if (!selectedTicket || !replyText.trim()) return;
    setReplying(true);
    try {
      await postJson(`/api/tenants/${tenantId}/tickets/${selectedTicket.id}/reply`, { body: replyText });
      setReplyText("");
      toast("Reply sent", "success");
      await openDetail(selectedTicket);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to send reply", "error");
    } finally {
      setReplying(false);
    }
  }, [selectedTicket, replyText, tenantId, toast, openDetail]);

  const handleCreate = useCallback(async () => {
    if (!createForm.subject.trim()) return;
    setCreating(true);
    try {
      await postJson(`/api/tenants/${tenantId}/tickets`, createForm);
      toast("Ticket created", "success");
      setShowCreate(false);
      setCreateForm({ subject: "", body: "", severity: "S3", component: "Other" });
      void fetchTickets();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to create ticket", "error");
    } finally {
      setCreating(false);
    }
  }, [createForm, tenantId, toast, fetchTickets]);

  const handleClose = useCallback(async (ticketId: string) => {
    try {
      await postJson(`/api/tenants/${tenantId}/tickets/${ticketId}/close`, {});
      toast("Ticket closed", "success");
      setCloseConfirm(null);
      setDetailOpen(false);
      void fetchTickets();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to close ticket", "error");
    }
  }, [tenantId, toast, fetchTickets]);

  const columns: Column<Ticket>[] = [
    { key: "ticket_number", header: "#", render: (r) => <code>#{r.ticket_number}</code> },
    { key: "subject", header: "Subject" },
    { key: "severity", header: "Severity", render: (r) => <StatusBadge status={r.severity} variant={SEVERITY_VARIANT[r.severity] ?? "neutral"} size="sm" /> },
    { key: "status", header: "Status", render: (r) => <StatusBadge status={r.status.replace(/_/g, " ")} variant={STATUS_VARIANT[r.status] ?? "neutral"} size="sm" /> },
    { key: "component", header: "Component" },
    { key: "created_at", header: "Created", render: (r) => formatDate(r.created_at) },
    { key: "updated_at", header: "Updated", render: (r) => formatDate(r.updated_at) },
    { key: "sla_due_at", header: "SLA Due", render: (r) => (
      <span style={{ color: isSlaOverdue(r.sla_due_at, r.status) ? "var(--critical)" : "inherit", fontWeight: isSlaOverdue(r.sla_due_at, r.status) ? 700 : 400 }}>
        {formatDate(r.sla_due_at)}
      </span>
    )},
  ];

  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />
      <PageHeader
        title="Support"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Operations" }, { label: "Support" }]}
        actions={[{ label: "New Ticket", onClick: () => setShowCreate(true), variant: "primary" as const }]}
      />

      <FilterBar filters={filterDefs} values={filters} onChange={(k, v) => { setFilters(prev => ({ ...prev, [k]: v })); setPage(1); }} onClear={() => { setFilters({ status: "", severity: "", search: "" }); setPage(1); }} />

      {!loading && tickets.length === 0 && !filters.search && !filters.status ? (
        <EmptyState title="No tickets" description="Support tickets will appear here once created." icon="&#x2709;" />
      ) : (
        <DataTable<Ticket> columns={columns} data={tickets} loading={loading} emptyMessage="No tickets match your filters" page={page} pageSize={PAGE_SIZE} total={total} onPageChange={setPage} onRowClick={openDetail} striped />
      )}

      {/* Create dialog */}
      {showCreate && (
        <ConfirmDialog title="Create Support Ticket" onConfirm={handleCreate} onCancel={() => setShowCreate(false)} confirmLabel={creating ? "Creating..." : "Create"}>
          <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
            <input placeholder="Subject" value={createForm.subject} onChange={(e) => setCreateForm(f => ({ ...f, subject: e.target.value }))} style={{ padding: "0.5rem", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-light)", fontSize: "0.875rem" }} />
            <textarea placeholder="Description" value={createForm.body} onChange={(e) => setCreateForm(f => ({ ...f, body: e.target.value }))} rows={4} style={{ padding: "0.5rem", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-light)", fontSize: "0.875rem", resize: "vertical" }} />
            <div style={{ display: "flex", gap: "0.75rem" }}>
              <select value={createForm.severity} onChange={(e) => setCreateForm(f => ({ ...f, severity: e.target.value }))} style={{ flex: 1, padding: "0.5rem", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-light)" }}>
                <option value="S1">S1 - Critical</option><option value="S2">S2 - High</option><option value="S3">S3 - Medium</option><option value="S4">S4 - Low</option>
              </select>
              <select value={createForm.component} onChange={(e) => setCreateForm(f => ({ ...f, component: e.target.value }))} style={{ flex: 1, padding: "0.5rem", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-light)" }}>
                <option value="SM">Site Manager</option><option value="Agent">Agent</option><option value="Skill">Skill</option><option value="CC">Central Command</option><option value="Billing">Billing</option><option value="Other">Other</option>
              </select>
            </div>
          </div>
        </ConfirmDialog>
      )}

      {/* Close confirm */}
      {closeConfirm && (
        <ConfirmDialog title="Close Ticket" onConfirm={() => void handleClose(closeConfirm)} onCancel={() => setCloseConfirm(null)} confirmLabel="Close Ticket" variant="danger">
          Are you sure you want to close this ticket?
        </ConfirmDialog>
      )}

      {/* Detail panel */}
      <DetailPanel open={detailOpen} onClose={() => { setDetailOpen(false); setSelectedTicket(null); }} title={selectedTicket ? `#${selectedTicket.ticket_number} ${selectedTicket.subject}` : "Ticket"} width={560}>
        {detailLoading ? (
          <div style={{ display: "flex", justifyContent: "center", padding: "2rem" }}><Spinner size="md" /></div>
        ) : selectedTicket ? (
          <>
            <div style={cardStyle}>
              <div style={{ display: "flex", gap: "0.5rem", marginBottom: "0.5rem" }}>
                <StatusBadge status={selectedTicket.severity} variant={SEVERITY_VARIANT[selectedTicket.severity] ?? "neutral"} size="sm" />
                <StatusBadge status={selectedTicket.status.replace(/_/g, " ")} variant={STATUS_VARIANT[selectedTicket.status] ?? "neutral"} size="sm" />
                <StatusBadge status={selectedTicket.component} variant="neutral" size="sm" />
              </div>
              <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
                Created {formatDate(selectedTicket.created_at)}
                {selectedTicket.sla_due_at && <> | SLA: <span style={{ color: isSlaOverdue(selectedTicket.sla_due_at, selectedTicket.status) ? "var(--critical)" : "inherit" }}>{formatDate(selectedTicket.sla_due_at)}</span></>}
              </div>
            </div>

            {/* Messages */}
            <div style={{ fontSize: "0.75rem", fontWeight: 600, color: "var(--text-secondary)", marginBottom: "0.5rem", textTransform: "uppercase" }}>Messages</div>
            {messages.length === 0 ? (
              <div style={{ fontSize: "0.8125rem", color: "var(--text-muted)", padding: "0.5rem 0" }}>No messages yet.</div>
            ) : messages.map((m) => (
              <div key={m.id} style={m.is_internal ? internalBubble : msgBubble}>
                <div style={{ fontSize: "0.6875rem", color: "var(--text-muted)", marginBottom: "0.25rem" }}>
                  {m.author_email} {m.is_internal && <strong>(internal)</strong>} - {formatDate(m.created_at)}
                </div>
                <div>{m.body}</div>
              </div>
            ))}

            {/* Reply */}
            {selectedTicket.status !== "closed" && (
              <div style={replyBox}>
                <textarea value={replyText} onChange={(e) => setReplyText(e.target.value)} placeholder="Type your reply..." rows={3} style={{ padding: "0.5rem", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-light)", fontSize: "0.8125rem", resize: "vertical" }} />
                <div style={{ display: "flex", gap: "0.5rem" }}>
                  <button className="btn btn-sm btn-primary" onClick={() => void handleReply()} disabled={replying || !replyText.trim()}>
                    {replying ? "Sending..." : "Reply"}
                  </button>
                  <button className="btn btn-sm" onClick={() => setCloseConfirm(selectedTicket.id)}>Close Ticket</button>
                </div>
              </div>
            )}
          </>
        ) : null}
      </DetailPanel>
    </div>
  );
}
