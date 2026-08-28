import { type CSSProperties, useCallback, useEffect, useMemo, useState } from "react";
import PageHeader from "../components/PageHeader";
import FilterBar, { type FilterDef } from "../components/FilterBar";
import DataTable, { type Column } from "../components/DataTable";
import DetailPanel from "../components/DetailPanel";
import ConfirmDialog from "../components/ConfirmDialog";
import StatusBadge from "../components/StatusBadge";
import EmptyState from "../components/EmptyState";
import Toast from "../components/Toast";
import Spinner from "../components/Spinner";
import { useToast } from "../components/useToast";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../useAuth";
import { getJson, postJson } from "../api";
import type { PaginatedResponse, Tenant } from "../types";

/* ── Extended types for this page ───────────────── */

interface TenantDetail extends Tenant {
  billing_country: string;
  currency: string;
  admin_email: string;
  subscription?: {
    plan: string;
    status: string;
    node_commit: number;
    node_high_water: number;
    billing_cycle: string;
    current_period_start: string;
    current_period_end: string;
  };
  users_count: number;
  sites: Array<{ id: string; name: string; node_count: number }>;
}

interface CreateTenantPayload {
  name: string;
  slug: string;
  billing_country: string;
  currency: string;
  plan: string;
  node_commit: number;
  admin_email: string;
}

/* ── Constants ──────────────────────────────────── */

const PAGE_SIZE = 20;
const POLL_INTERVAL = 30000;

const COUNTRY_OPTIONS = [
  { value: "US", label: "United States" },
  { value: "IN", label: "India" },
  { value: "EU", label: "European Union" },
  { value: "UK", label: "United Kingdom" },
];

const COUNTRY_CURRENCY: Record<string, string> = {
  US: "USD",
  IN: "INR",
  EU: "EUR",
  UK: "USD",
};

const PLAN_OPTIONS = [
  { value: "observe", label: "Observe" },
  { value: "approve", label: "Approve" },
  { value: "enterprise", label: "Enterprise" },
];

const STATUS_VARIANT: Record<string, "success" | "warning" | "critical" | "info" | "neutral"> = {
  active: "success",
  pending: "info",
  suspended: "critical",
  delinquent: "warning",
};

/* ── Filter definitions ─────────────────────────── */

const filterDefs: FilterDef[] = [
  { key: "search", label: "Search", type: "text", placeholder: "Search tenants..." },
  {
    key: "status",
    label: "Status",
    type: "select",
    options: [
      { value: "active", label: "Active" },
      { value: "suspended", label: "Suspended" },
      { value: "pending", label: "Pending" },
      { value: "delinquent", label: "Delinquent" },
    ],
  },
  {
    key: "plan",
    label: "Plan",
    type: "select",
    options: PLAN_OPTIONS,
  },
];

/* ── Styles ─────────────────────────────────────── */

const modalOverlay: CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "var(--bg-overlay)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 2000,
};

const modalCard: CSSProperties = {
  background: "var(--bg-card)",
  borderRadius: "var(--radius-lg)",
  boxShadow: "var(--shadow-lg)",
  padding: "1.5rem",
  maxWidth: 520,
  width: "90vw",
  maxHeight: "85vh",
  overflow: "auto",
};

const formGroup: CSSProperties = {
  marginBottom: "1rem",
};

const labelStyle: CSSProperties = {
  display: "block",
  fontSize: "0.8125rem",
  fontWeight: 600,
  color: "var(--text-secondary)",
  marginBottom: "0.375rem",
};

const errorText: CSSProperties = {
  fontSize: "0.75rem",
  color: "var(--status-critical)",
  marginTop: "0.25rem",
};

const sectionTitle: CSSProperties = {
  fontSize: "0.8125rem",
  fontWeight: 600,
  color: "var(--text-secondary)",
  textTransform: "uppercase",
  letterSpacing: "0.04em",
  marginBottom: "0.75rem",
  marginTop: "1.25rem",
  borderBottom: "1px solid var(--border-light)",
  paddingBottom: "0.375rem",
};

const detailRow: CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  padding: "0.375rem 0",
  fontSize: "0.8125rem",
  borderBottom: "1px solid var(--border-light)",
};

const detailLabel: CSSProperties = {
  color: "var(--text-secondary)",
  fontWeight: 500,
};

const detailValue: CSSProperties = {
  color: "var(--text-primary)",
  fontWeight: 500,
  textAlign: "right",
};

/* ── Helpers ────────────────────────────────────── */

function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/\s+/g, "-")
    .replace(/[^a-z0-9-]/g, "")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

function formatDate(iso: string): string {
  if (!iso) return "--";
  return new Date(iso).toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function isValidEmail(email: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
}

/* ── Component ──────────────────────────────────── */

export default function TenantManagement() {
  const { toasts, toast, dismiss } = useToast();
  const navigate = useNavigate();
  const { user } = useAuth();
  const [entering, setEntering] = useState<string | null>(null);

  /* ── List state ──────────────────────────────── */
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [filters, setFilters] = useState<Record<string, string>>({ search: "", status: "", plan: "" });

  /* ── Detail state ────────────────────────────── */
  const [selectedTenant, setSelectedTenant] = useState<TenantDetail | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);

  /* ── Create state ────────────────────────────── */
  const [createOpen, setCreateOpen] = useState(false);
  const [createLoading, setCreateLoading] = useState(false);
  const [form, setForm] = useState<CreateTenantPayload>({
    name: "",
    slug: "",
    billing_country: "US",
    currency: "USD",
    plan: "observe",
    node_commit: 10,
    admin_email: "",
  });
  const [formErrors, setFormErrors] = useState<Record<string, string>>({});
  const [slugManual, setSlugManual] = useState(false);

  /* ── Suspend / reactivate state ──────────────── */
  const [suspendOpen, setSuspendOpen] = useState(false);
  const [suspendReason, setSuspendReason] = useState("");
  const [suspendLoading, setSuspendLoading] = useState(false);
  const [reactivateOpen, setReactivateOpen] = useState(false);
  const [reactivateLoading, setReactivateLoading] = useState(false);

  /* ── Fetch list ──────────────────────────────── */
  const fetchTenants = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (filters.search) params.set("search", filters.search);
      if (filters.status) params.set("status", filters.status);
      if (filters.plan) params.set("plan", filters.plan);
      params.set("page", String(page));
      params.set("page_size", String(PAGE_SIZE));
      const res = await getJson<PaginatedResponse<Tenant>>(
        `/api/admin/tenants?${params.toString()}`,
      );
      setTenants(res.items);
      setTotal(res.total);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load tenants", "error");
    } finally {
      setLoading(false);
    }
  }, [filters, page, toast]);

  useEffect(() => {
    setLoading(true);
    void fetchTenants();
  }, [fetchTenants]);

  /* ── Polling ─────────────────────────────────── */
  useEffect(() => {
    const timer = setInterval(() => void fetchTenants(), POLL_INTERVAL);
    return () => clearInterval(timer);
  }, [fetchTenants]);

  /* ── Fetch detail ────────────────────────────── */
  const openDetail = useCallback(
    async (tenant: Tenant) => {
      setDetailOpen(true);
      setDetailLoading(true);
      try {
        const detail = await getJson<TenantDetail>(`/api/admin/tenants/${tenant.id}`);
        setSelectedTenant(detail);
      } catch (err) {
        toast(err instanceof Error ? err.message : "Failed to load tenant detail", "error");
        setDetailOpen(false);
      } finally {
        setDetailLoading(false);
      }
    },
    [toast],
  );

  /* ── Create tenant ───────────────────────────── */
  const validateForm = useCallback((): boolean => {
    const errors: Record<string, string> = {};
    if (!form.name.trim()) errors.name = "Name is required";
    if (!form.slug.trim()) {
      errors.slug = "Slug is required";
    } else if (!/^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/.test(form.slug)) {
      errors.slug = "Slug must be lowercase alphanumeric with optional hyphens";
    }
    if (!form.admin_email.trim()) {
      errors.admin_email = "Admin email is required";
    } else if (!isValidEmail(form.admin_email)) {
      errors.admin_email = "Invalid email format";
    }
    if (form.node_commit < 1) errors.node_commit = "Node commit must be at least 1";
    setFormErrors(errors);
    return Object.keys(errors).length === 0;
  }, [form]);

  const handleCreate = useCallback(async () => {
    if (!validateForm()) return;
    setCreateLoading(true);
    try {
      await postJson<Tenant>("/api/admin/tenants", form);
      toast("Tenant created successfully", "success");
      setCreateOpen(false);
      resetForm();
      void fetchTenants();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to create tenant", "error");
    } finally {
      setCreateLoading(false);
    }
  }, [form, validateForm, toast, fetchTenants]);

  const resetForm = () => {
    setForm({
      name: "",
      slug: "",
      billing_country: "US",
      currency: "USD",
      plan: "observe",
      node_commit: 10,
      admin_email: "",
    });
    setFormErrors({});
    setSlugManual(false);
  };

  /* ── Suspend ─────────────────────────────────── */
  const handleSuspend = useCallback(async () => {
    if (!selectedTenant || !suspendReason.trim()) return;
    setSuspendLoading(true);
    try {
      await postJson(`/api/admin/tenants/${selectedTenant.id}/suspend`, {
        reason: suspendReason,
      });
      toast("Tenant suspended", "success");
      setSuspendOpen(false);
      setSuspendReason("");
      setDetailOpen(false);
      setSelectedTenant(null);
      void fetchTenants();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to suspend tenant", "error");
    } finally {
      setSuspendLoading(false);
    }
  }, [selectedTenant, suspendReason, toast, fetchTenants]);

  /* ── Reactivate ──────────────────────────────── */
  const handleReactivate = useCallback(async () => {
    if (!selectedTenant) return;
    setReactivateLoading(true);
    try {
      await postJson(`/api/admin/tenants/${selectedTenant.id}/reactivate`, {});
      toast("Tenant reactivated", "success");
      setReactivateOpen(false);
      setDetailOpen(false);
      setSelectedTenant(null);
      void fetchTenants();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to reactivate tenant", "error");
    } finally {
      setReactivateLoading(false);
    }
  }, [selectedTenant, toast, fetchTenants]);

  /* ── Table columns ───────────────────────────── */
  /* ── Entering a tenant ───────────────────────────
     Listing and entering are different acts. The server decides: a
     platform_super_admin passes on break-glass, a platform_support caller
     is refused until a request has been approved. The UI asks first so it
     can offer the request instead of dropping the user into a tenant that
     will 403 on every panel. */
  const enterTenant = useCallback(
    async (t: Tenant) => {
      // Break-glass is not gated: probing the status endpoint first meant
      // a wasted round trip for the demo's main persona — and a FAILING
      // status endpoint wrongly blocked a super admin's entry (review,
      // performance pass). The server enforces every request either way.
      if (user?.role === "platform_super_admin") {
        navigate(`/t/${t.id}/dashboard`);
        return;
      }
      setEntering(t.id);
      try {
        const status = await getJson<{ active: boolean; pending?: boolean }>(
          `/api/admin/support-access/${t.id}`,
        );
        if (status.active) {
          navigate(`/t/${t.id}/dashboard`);
          return;
        }
        if (status.pending) {
          toast("Access already requested — waiting on an approver", "info");
          return;
        }
        await postJson(`/api/admin/support-access/${t.id}/request`, {
          reason: `Console entry to ${t.name}`,
        });
        toast("Access requested — a platform admin must approve it", "success");
      } catch (e) {
        toast((e as Error).message, "error");
      } finally {
        setEntering(null);
      }
    },
    [navigate, toast, user],
  );

  const columns = useMemo<Column<Tenant>[]>(
    () => [
      { key: "name", header: "Name", sortKey: "name" },
      { key: "slug", header: "Slug", render: (r) => <code style={{ fontSize: "0.8125rem" }}>{r.slug}</code> },
      {
        key: "status",
        header: "Status",
        render: (r) => (
          <StatusBadge status={r.status} variant={STATUS_VARIANT[r.status] ?? "neutral"} size="sm" />
        ),
      },
      { key: "plan", header: "Plan", render: (r) => <span style={{ textTransform: "capitalize" }}>{r.plan}</span> },
      { key: "region", header: "Region" },
      {
        key: "created_at",
        header: "Created At",
        sortKey: "created_at",
        render: (r) => formatDate(r.created_at),
      },
      {
        // Entering a tenant is a separate act from seeing it listed, so it
        // is a separate control — not a side effect of clicking the row,
        // which opens the registry detail.
        key: "enter",
        header: "",
        render: (r) => (
          <button
            className="btn btn-sm"
            onClick={(e) => {
              e.stopPropagation();
              void enterTenant(r);
            }}
            disabled={entering === r.id}
          >
            {entering === r.id ? "Checking…" : "Enter"}
          </button>
        ),
      },
    ],
    [enterTenant, entering],
  );

  /* ── Filter handlers ─────────────────────────── */
  const handleFilterChange = useCallback((key: string, value: string) => {
    setFilters((prev) => ({ ...prev, [key]: value }));
    setPage(1);
  }, []);

  const handleFilterClear = useCallback(() => {
    setFilters({ search: "", status: "", plan: "" });
    setPage(1);
  }, []);

  /* ── Render ──────────────────────────────────── */
  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />

      <PageHeader
        title="Tenants"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Admin" }, { label: "Tenants" }]}
        actions={
          <button className="btn btn-primary" onClick={() => setCreateOpen(true)}>
            Create Tenant
          </button>
        }
      />

      <FilterBar
        filters={filterDefs}
        values={filters}
        onChange={handleFilterChange}
        onClear={handleFilterClear}
      />

      {!loading && tenants.length === 0 && !filters.search && !filters.status && !filters.plan ? (
        <EmptyState
          title="No tenants yet"
          description="Get started by creating your first tenant."
          actionLabel="Create Tenant"
          onAction={() => setCreateOpen(true)}
          icon="&#x2302;"
        />
      ) : (
        <DataTable<Tenant>
          columns={columns}
          data={tenants}
          loading={loading}
          emptyMessage="No tenants match your filters"
          page={page}
          pageSize={PAGE_SIZE}
          total={total}
          onPageChange={setPage}
          onRowClick={openDetail}
          striped
        />
      )}

      {/* ── Detail Panel ───────────────────────── */}
      <DetailPanel
        open={detailOpen}
        onClose={() => {
          setDetailOpen(false);
          setSelectedTenant(null);
        }}
        title={selectedTenant?.name ?? "Tenant Details"}
        subtitle={selectedTenant?.slug}
        width={520}
      >
        {detailLoading ? (
          <div style={{ display: "flex", justifyContent: "center", padding: "2rem" }}>
            <Spinner size="md" />
          </div>
        ) : selectedTenant ? (
          <>
            <div style={sectionTitle}>Tenant Info</div>
            <div style={detailRow}>
              <span style={detailLabel}>Name</span>
              <span style={detailValue}>{selectedTenant.name}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Slug</span>
              <span style={detailValue}>
                <code>{selectedTenant.slug}</code>
              </span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Status</span>
              <span style={detailValue}>
                <StatusBadge
                  status={selectedTenant.status}
                  variant={STATUS_VARIANT[selectedTenant.status] ?? "neutral"}
                  size="sm"
                />
              </span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Region</span>
              <span style={detailValue}>{selectedTenant.region}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Billing Country</span>
              <span style={detailValue}>{selectedTenant.billing_country ?? "--"}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Currency</span>
              <span style={detailValue}>{selectedTenant.currency ?? "--"}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Contact Email</span>
              <span style={detailValue}>{selectedTenant.contact_email}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Created</span>
              <span style={detailValue}>{formatDate(selectedTenant.created_at)}</span>
            </div>

            {selectedTenant.subscription && (
              <>
                <div style={sectionTitle}>Subscription</div>
                <div style={detailRow}>
                  <span style={detailLabel}>Plan</span>
                  <span style={{ ...detailValue, textTransform: "capitalize" }}>
                    {selectedTenant.subscription.plan}
                  </span>
                </div>
                <div style={detailRow}>
                  <span style={detailLabel}>Status</span>
                  <span style={detailValue}>{selectedTenant.subscription.status}</span>
                </div>
                <div style={detailRow}>
                  <span style={detailLabel}>Node Commit</span>
                  <span style={detailValue}>{selectedTenant.subscription.node_commit}</span>
                </div>
                <div style={detailRow}>
                  <span style={detailLabel}>High Water</span>
                  <span style={detailValue}>{selectedTenant.subscription.node_high_water}</span>
                </div>
                <div style={detailRow}>
                  <span style={detailLabel}>Billing Cycle</span>
                  <span style={{ ...detailValue, textTransform: "capitalize" }}>
                    {selectedTenant.subscription.billing_cycle}
                  </span>
                </div>
                <div style={detailRow}>
                  <span style={detailLabel}>Period</span>
                  <span style={detailValue}>
                    {formatDate(selectedTenant.subscription.current_period_start)} -{" "}
                    {formatDate(selectedTenant.subscription.current_period_end)}
                  </span>
                </div>
              </>
            )}

            <div style={sectionTitle}>Users &amp; Sites</div>
            <div style={detailRow}>
              <span style={detailLabel}>Users Count</span>
              <span style={detailValue}>{selectedTenant.users_count ?? 0}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Sites</span>
              <span style={detailValue}>{selectedTenant.sites?.length ?? 0}</span>
            </div>
            {selectedTenant.sites && selectedTenant.sites.length > 0 && (
              <div style={{ marginTop: "0.5rem" }}>
                {selectedTenant.sites.map((site) => (
                  <div
                    key={site.id}
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      padding: "0.25rem 0.5rem",
                      fontSize: "0.8125rem",
                      background: "var(--bg-primary)",
                      borderRadius: "var(--radius-md)",
                      marginBottom: "0.25rem",
                    }}
                  >
                    <span>{site.name}</span>
                    <span style={{ color: "var(--text-secondary)" }}>{site.node_count} nodes</span>
                  </div>
                ))}
              </div>
            )}

            {/* Action buttons */}
            <div style={{ marginTop: "1.5rem", display: "flex", gap: "0.5rem" }}>
              {selectedTenant.status !== "suspended" ? (
                <button
                  className="btn btn-danger"
                  onClick={() => setSuspendOpen(true)}
                >
                  Suspend Tenant
                </button>
              ) : (
                <button
                  className="btn btn-primary"
                  onClick={() => setReactivateOpen(true)}
                >
                  Reactivate Tenant
                </button>
              )}
            </div>
          </>
        ) : null}
      </DetailPanel>

      {/* ── Suspend Confirm Dialog ─────────────── */}
      <ConfirmDialog
        open={suspendOpen}
        title="Suspend Tenant"
        message={
          suspendReason.trim()
            ? `Suspend "${selectedTenant?.name}"? This will restrict console access. Diagnosis agents remain active.`
            : `Suspend "${selectedTenant?.name}"? Please provide a reason below.`
        }
        confirmLabel="Suspend"
        variant="danger"
        onConfirm={handleSuspend}
        onCancel={() => {
          setSuspendOpen(false);
          setSuspendReason("");
        }}
        loading={suspendLoading}
      />
      {/* Reason textarea rendered alongside the dialog via portal workaround:
          we render it in a separate overlay only when suspendOpen is true */}
      {suspendOpen && (
        <div
          style={{
            position: "fixed",
            zIndex: 2001,
            top: "50%",
            left: "50%",
            transform: "translate(-50%, -50%)",
            width: "90vw",
            maxWidth: 420,
            pointerEvents: "none",
          }}
        >
          <div style={{ pointerEvents: "auto", paddingTop: "5.5rem" }}>
            <label style={labelStyle}>Reason for suspension</label>
            <textarea
              className="input"
              value={suspendReason}
              onChange={(e) => setSuspendReason(e.target.value)}
              rows={3}
              placeholder="Explain why this tenant is being suspended..."
              style={{ width: "100%", resize: "vertical" }}
            />
          </div>
        </div>
      )}

      {/* ── Reactivate Confirm Dialog ──────────── */}
      <ConfirmDialog
        open={reactivateOpen}
        title="Reactivate Tenant"
        message={`Reactivate "${selectedTenant?.name}"? This will restore full console access.`}
        confirmLabel="Reactivate"
        variant="default"
        onConfirm={handleReactivate}
        onCancel={() => setReactivateOpen(false)}
        loading={reactivateLoading}
      />

      {/* ── Create Tenant Modal ────────────────── */}
      {createOpen && (
        <div style={modalOverlay} onClick={() => { setCreateOpen(false); resetForm(); }} role="presentation">
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="create-tenant-title"
            style={modalCard}
            onClick={(e) => e.stopPropagation()}
          >
            <h3 id="create-tenant-title" style={{ margin: "0 0 1rem 0", fontSize: "1.125rem", fontWeight: 600 }}>
              Create Tenant
            </h3>

            {/* Name */}
            <div style={formGroup}>
              <label style={labelStyle}>Name *</label>
              <input
                className="input"
                type="text"
                value={form.name}
                onChange={(e) => {
                  const name = e.target.value;
                  setForm((prev) => ({
                    ...prev,
                    name,
                    slug: slugManual ? prev.slug : slugify(name),
                  }));
                  setFormErrors((prev) => ({ ...prev, name: "" }));
                }}
                placeholder="Acme Corporation"
                style={{ width: "100%" }}
              />
              {formErrors.name && <div style={errorText}>{formErrors.name}</div>}
            </div>

            {/* Slug */}
            <div style={formGroup}>
              <label style={labelStyle}>Slug *</label>
              <input
                className="input"
                type="text"
                value={form.slug}
                onChange={(e) => {
                  setSlugManual(true);
                  setForm((prev) => ({ ...prev, slug: e.target.value }));
                  setFormErrors((prev) => ({ ...prev, slug: "" }));
                }}
                placeholder="acme-corporation"
                style={{ width: "100%", fontFamily: "monospace" }}
              />
              {formErrors.slug && <div style={errorText}>{formErrors.slug}</div>}
              {!slugManual && form.name && (
                <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", marginTop: "0.25rem" }}>
                  Auto-generated from name
                </div>
              )}
            </div>

            {/* Billing Country */}
            <div style={formGroup}>
              <label style={labelStyle}>Billing Country *</label>
              <select
                className="select"
                value={form.billing_country}
                onChange={(e) => {
                  const country = e.target.value;
                  setForm((prev) => ({
                    ...prev,
                    billing_country: country,
                    currency: COUNTRY_CURRENCY[country] ?? "USD",
                  }));
                }}
                style={{ width: "100%" }}
              >
                {COUNTRY_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </div>

            {/* Currency (auto) */}
            <div style={formGroup}>
              <label style={labelStyle}>Currency</label>
              <input
                className="input"
                type="text"
                value={form.currency}
                readOnly
                style={{ width: "100%", background: "var(--bg-primary)", cursor: "not-allowed" }}
              />
              <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", marginTop: "0.25rem" }}>
                Auto-set from billing country
              </div>
            </div>

            {/* Plan */}
            <div style={formGroup}>
              <label style={labelStyle}>Plan *</label>
              <select
                className="select"
                value={form.plan}
                onChange={(e) => setForm((prev) => ({ ...prev, plan: e.target.value }))}
                style={{ width: "100%" }}
              >
                {PLAN_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </div>

            {/* Node Commit */}
            <div style={formGroup}>
              <label style={labelStyle}>Node Commit *</label>
              <input
                className="input"
                type="number"
                min={1}
                value={form.node_commit}
                onChange={(e) => {
                  setForm((prev) => ({ ...prev, node_commit: parseInt(e.target.value, 10) || 0 }));
                  setFormErrors((prev) => ({ ...prev, node_commit: "" }));
                }}
                style={{ width: "100%" }}
              />
              {formErrors.node_commit && <div style={errorText}>{formErrors.node_commit}</div>}
            </div>

            {/* Admin Email */}
            <div style={formGroup}>
              <label style={labelStyle}>Admin Email *</label>
              <input
                className="input"
                type="email"
                value={form.admin_email}
                onChange={(e) => {
                  setForm((prev) => ({ ...prev, admin_email: e.target.value }));
                  setFormErrors((prev) => ({ ...prev, admin_email: "" }));
                }}
                placeholder="admin@acme.com"
                style={{ width: "100%" }}
              />
              {formErrors.admin_email && <div style={errorText}>{formErrors.admin_email}</div>}
            </div>

            {/* Actions */}
            <div style={{ display: "flex", justifyContent: "flex-end", gap: "0.5rem", marginTop: "1.25rem" }}>
              <button
                className="btn"
                onClick={() => {
                  setCreateOpen(false);
                  resetForm();
                }}
                disabled={createLoading}
              >
                Cancel
              </button>
              <button className="btn btn-primary" onClick={handleCreate} disabled={createLoading}>
                {createLoading && <Spinner size="sm" />}
                Create Tenant
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
