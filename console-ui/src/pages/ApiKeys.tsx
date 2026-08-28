import { type CSSProperties, useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import DataTable, { type Column } from "../components/DataTable";
import StatusBadge from "../components/StatusBadge";
import ConfirmDialog from "../components/ConfirmDialog";
import EmptyState from "../components/EmptyState";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";
import { getJson, postJson } from "../api";

/* ── Types ────────────────────────────────────────── */

interface ApiKeyRow {
  id: string;
  name: string;
  key_prefix: string;
  key?: string;
  scope: string;
  status: string;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
}

/* ── Styles ───────────────────────────────────────── */

const keyRevealBox: CSSProperties = {
  background: "#fef3c7", border: "1px solid #f59e0b", borderRadius: "var(--radius-md)",
  padding: "1rem", marginBottom: "1rem", fontSize: "0.8125rem",
};

const SCOPE_VARIANT: Record<string, "success" | "warning" | "critical" | "info"> = {
  read: "info", write: "warning", admin: "critical",
};

/* ── Helpers ──────────────────────────────────────── */

function formatDate(iso: string | null): string {
  if (!iso) return "--";
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

/* ── Component ────────────────────────────────────── */

const PAGE_SIZE = 20;

export default function ApiKeys() {
  const { toasts, toast, dismiss } = useToast();
  const [keys, setKeys] = useState<ApiKeyRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [createForm, setCreateForm] = useState({ name: "", scope: "read", expires_in_days: "" });
  const [creating, setCreating] = useState(false);
  const [newKey, setNewKey] = useState<string | null>(null);
  const [revokeConfirm, setRevokeConfirm] = useState<string | null>(null);

  const { tenantId = "" } = useParams<{ tenantId: string }>();

  const fetchKeys = useCallback(async () => {
    setLoading(true);
    try {
      const res = await getJson<{ items: ApiKeyRow[]; total: number }>(
        `/api/tenants/${tenantId}/api-keys?page=${page}&page_size=${PAGE_SIZE}`,
      );
      setKeys(res.items);
      setTotal(res.total);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load API keys", "error");
    } finally {
      setLoading(false);
    }
  }, [page, tenantId, toast]);

  useEffect(() => { void fetchKeys(); }, [fetchKeys]);

  const handleCreate = useCallback(async () => {
    if (!createForm.name.trim()) return;
    setCreating(true);
    try {
      const body: Record<string, unknown> = { name: createForm.name, scope: createForm.scope };
      if (createForm.expires_in_days) body.expires_in_days = parseInt(createForm.expires_in_days);
      const res = await postJson<ApiKeyRow>(`/api/tenants/${tenantId}/api-keys`, body);
      setNewKey(res.key ?? null);
      setShowCreate(false);
      setCreateForm({ name: "", scope: "read", expires_in_days: "" });
      toast("API key created", "success");
      void fetchKeys();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Create failed", "error");
    } finally {
      setCreating(false);
    }
  }, [createForm, tenantId, toast, fetchKeys]);

  const handleRevoke = useCallback(async (keyId: string) => {
    try {
      await postJson(`/api/tenants/${tenantId}/api-keys/${keyId}/revoke`, {});
      toast("API key revoked", "success");
      setRevokeConfirm(null);
      void fetchKeys();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Revoke failed", "error");
    }
  }, [tenantId, toast, fetchKeys]);

  const columns: Column<ApiKeyRow>[] = [
    { key: "name", header: "Name" },
    { key: "key_prefix", header: "Key", render: (r) => <code>{r.key_prefix}...</code> },
    { key: "scope", header: "Scope", render: (r) => <StatusBadge status={r.scope} variant={SCOPE_VARIANT[r.scope] ?? "neutral"} size="sm" /> },
    { key: "status", header: "Status", render: (r) => <StatusBadge status={r.status} variant={r.status === "active" ? "success" : "neutral"} size="sm" /> },
    { key: "created_at", header: "Created", render: (r) => formatDate(r.created_at) },
    { key: "expires_at", header: "Expires", render: (r) => formatDate(r.expires_at) },
    { key: "last_used_at", header: "Last Used", render: (r) => formatDate(r.last_used_at) },
    { key: "actions", header: "", render: (r) => r.status === "active" ? (
      <button className="btn btn-sm" style={{ color: "var(--critical)" }} onClick={(e) => { e.stopPropagation(); setRevokeConfirm(r.id); }}>Revoke</button>
    ) : null },
  ];

  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />
      <PageHeader
        title="API Keys"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Settings" }, { label: "API Keys" }]}
        actions={[{ label: "Generate Key", onClick: () => setShowCreate(true), variant: "primary" as const }]}
      />

      {/* New key reveal */}
      {newKey && (
        <div style={keyRevealBox}>
          <strong>New API key created.</strong> Copy it now -- it won't be shown again.
          <div style={{ marginTop: "0.5rem", fontFamily: "var(--font-mono, monospace)", wordBreak: "break-all" }}>{newKey}</div>
          <button className="btn btn-sm" style={{ marginTop: "0.5rem" }} onClick={() => { void navigator.clipboard.writeText(newKey); toast("Copied", "success"); }}>Copy</button>
          <button className="btn btn-sm" style={{ marginTop: "0.5rem", marginLeft: "0.5rem" }} onClick={() => setNewKey(null)}>Dismiss</button>
        </div>
      )}

      {!loading && keys.length === 0 ? (
        <EmptyState title="No API keys" description="Generate an API key for programmatic access to HarkenIQ." icon="&#x26BF;" />
      ) : (
        <DataTable<ApiKeyRow> columns={columns} data={keys} loading={loading} emptyMessage="No API keys" page={page} pageSize={PAGE_SIZE} total={total} onPageChange={setPage} striped />
      )}

      {showCreate && (
        <ConfirmDialog title="Generate API Key" onConfirm={handleCreate} onCancel={() => setShowCreate(false)} confirmLabel={creating ? "Generating..." : "Generate"}>
          <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
            <input placeholder="Key name (e.g. CI/CD pipeline)" value={createForm.name} onChange={(e) => setCreateForm(f => ({ ...f, name: e.target.value }))} style={{ padding: "0.5rem", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-light)" }} />
            <select value={createForm.scope} onChange={(e) => setCreateForm(f => ({ ...f, scope: e.target.value }))} style={{ padding: "0.5rem", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-light)" }}>
              <option value="read">Read-only</option>
              <option value="write">Write</option>
              <option value="admin">Admin</option>
            </select>
            <input type="number" placeholder="Expires in days (optional)" value={createForm.expires_in_days} onChange={(e) => setCreateForm(f => ({ ...f, expires_in_days: e.target.value }))} style={{ padding: "0.5rem", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-light)" }} />
          </div>
        </ConfirmDialog>
      )}

      {revokeConfirm && (
        <ConfirmDialog title="Revoke API Key" onConfirm={() => void handleRevoke(revokeConfirm)} onCancel={() => setRevokeConfirm(null)} confirmLabel="Revoke" variant="danger">
          This will immediately invalidate the API key. Any integrations using it will stop working.
        </ConfirmDialog>
      )}
    </div>
  );
}
