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
import { getJson, postJson, getAccessToken } from "../api";
import { useAuth } from "../useAuth";
import type { PaginatedResponse } from "../types";

/* ── Types ──────────────────────────────────────── */

interface LicenseRecord {
  id: string;
  tenant_id: string;
  fingerprint: string;
  plan: string;
  node_commit: number;
  valid_from: string;
  valid_until: string;
  status: "active" | "revoked" | "expired";
  issued_by: string;
  issued_at: string;
  revoked_by?: string;
  revoked_at?: string;
  revoke_reason?: string;
}

interface ValidateResult {
  valid: boolean;
  payload?: {
    tenant: string;
    plan: string;
    node_commit: number;
    expires_at: string;
  };
  errors?: string[];
}

interface TenantOption {
  id: string;
  name: string;
  slug: string;
}

/* ── Constants ──────────────────────────────────── */

const PAGE_SIZE = 20;
const POLL_INTERVAL = 30000;

const STATUS_VARIANT: Record<string, "success" | "warning" | "critical" | "info" | "neutral"> = {
  active: "success",
  revoked: "critical",
  expired: "warning",
};

const PLAN_VARIANT: Record<string, "success" | "warning" | "critical" | "info" | "neutral"> = {
  observe: "info",
  approve: "warning",
  autonomy: "success",
  enterprise: "success",
};

const VALIDITY_OPTIONS = [
  { value: "6", label: "6 months" },
  { value: "12", label: "12 months" },
  { value: "24", label: "24 months" },
];

const PLAN_OPTIONS = [
  { value: "observe", label: "Observe" },
  { value: "approve", label: "Approve" },
  { value: "autonomy", label: "Autonomy" },
];

/* ── Filter definitions ─────────────────────────── */

const filterDefs: FilterDef[] = [
  {
    key: "status",
    label: "Status",
    type: "select",
    options: [
      { value: "active", label: "Active" },
      { value: "revoked", label: "Revoked" },
      { value: "expired", label: "Expired" },
    ],
  },
];

/* ── Styles ─────────────────────────────────────── */

const tabBar: CSSProperties = {
  display: "flex",
  gap: "0",
  borderBottom: "2px solid var(--border-color)",
  marginBottom: "1.25rem",
};

const tabBtn = (active: boolean): CSSProperties => ({
  padding: "0.625rem 1.25rem",
  fontSize: "0.875rem",
  fontWeight: active ? 600 : 400,
  color: active ? "var(--accent)" : "var(--text-secondary)",
  background: "none",
  border: "none",
  borderBottom: active ? "2px solid var(--accent)" : "2px solid transparent",
  marginBottom: "-2px",
  cursor: "pointer",
  transition: "color var(--transition), border-color var(--transition)",
});

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
  maxWidth: 480,
  width: "90vw",
};

const formGroup: CSSProperties = { marginBottom: "1rem" };

const labelStyle: CSSProperties = {
  display: "block",
  fontSize: "0.8125rem",
  fontWeight: 600,
  color: "var(--text-secondary)",
  marginBottom: "0.375rem",
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

const detailLabel: CSSProperties = { color: "var(--text-secondary)", fontWeight: 500 };
const detailValue: CSSProperties = { color: "var(--text-primary)", fontWeight: 500, textAlign: "right" };

const monoStyle: CSSProperties = {
  fontFamily: "monospace",
  fontSize: "0.8125rem",
  wordBreak: "break-all",
};

const validateResultBox: CSSProperties = {
  marginTop: "1rem",
  padding: "1rem",
  background: "var(--bg-primary)",
  border: "1px solid var(--border-color)",
  borderRadius: "var(--radius-md)",
};

/* ── Helpers ────────────────────────────────────── */

function formatDate(iso: string): string {
  if (!iso) return "--";
  return new Date(iso).toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function truncate(s: string, len: number): string {
  return s.length > len ? s.slice(0, len) + "..." : s;
}

/* ── Component ──────────────────────────────────── */

export default function LicenseManagement() {
  const { toasts, toast, dismiss } = useToast();
  const { user } = useAuth();
  const isPlatformAdmin = user?.is_platform_user ?? false;

  /* ── Tabs ────────────────────────────────────── */
  const [activeTab, setActiveTab] = useState<"licenses" | "validate">("licenses");

  /* ── Tenant context ──────────────────────────── */
  const [tenantId, setTenantId] = useState(user?.tenant_id ?? "");
  const [tenants, setTenants] = useState<TenantOption[]>([]);

  /* ── Licenses list state ─────────────────────── */
  const [licenses, setLicenses] = useState<LicenseRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [filters, setFilters] = useState<Record<string, string>>({ status: "" });

  /* ── Detail state ────────────────────────────── */
  const [selectedLicense, setSelectedLicense] = useState<LicenseRecord | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);

  /* ── Issue state ─────────────────────────────── */
  const [issueOpen, setIssueOpen] = useState(false);
  const [issueLoading, setIssueLoading] = useState(false);
  const [issuePlan, setIssuePlan] = useState("observe");
  const [issueNodeCommit, setIssueNodeCommit] = useState(10);
  const [issueValidity, setIssueValidity] = useState("12");

  /* ── Revoke state ────────────────────────────── */
  const [revokeOpen, setRevokeOpen] = useState(false);
  const [revokeReason, setRevokeReason] = useState("");
  const [revokeLoading, setRevokeLoading] = useState(false);
  const [revokeTarget, setRevokeTarget] = useState<LicenseRecord | null>(null);

  /* ── Validate state ──────────────────────────── */
  const [licenseKey, setLicenseKey] = useState("");
  const [validateResult, setValidateResult] = useState<ValidateResult | null>(null);
  const [validateLoading, setValidateLoading] = useState(false);

  /* ── Fetch tenants for super admin ───────────── */
  useEffect(() => {
    if (!isPlatformAdmin) return;
    getJson<PaginatedResponse<TenantOption>>("/api/admin/tenants?page=1&page_size=200")
      .then((res) => {
        setTenants(res.items);
        if (!tenantId && res.items.length > 0) {
          setTenantId(res.items[0].id);
        }
      })
      .catch(() => {
        /* ignore - tenant selector just won't populate */
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPlatformAdmin]);

  /* ── Fetch licenses ──────────────────────────── */
  const fetchLicenses = useCallback(async () => {
    if (!tenantId) return;
    try {
      const params = new URLSearchParams();
      if (filters.status) params.set("status", filters.status);
      params.set("page", String(page));
      params.set("page_size", String(PAGE_SIZE));
      const res = await getJson<PaginatedResponse<LicenseRecord>>(
        `/api/tenants/${tenantId}/licenses?${params.toString()}`,
      );
      setLicenses(res.items);
      setTotal(res.total);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load licenses", "error");
    } finally {
      setLoading(false);
    }
  }, [tenantId, filters, page, toast]);

  useEffect(() => {
    if (activeTab === "licenses") {
      setLoading(true);
      void fetchLicenses();
    }
  }, [fetchLicenses, activeTab]);

  useEffect(() => {
    if (activeTab !== "licenses") return;
    const timer = setInterval(() => void fetchLicenses(), POLL_INTERVAL);
    return () => clearInterval(timer);
  }, [fetchLicenses, activeTab]);

  /* ── Open detail ─────────────────────────────── */
  const openDetail = useCallback(
    async (lic: LicenseRecord) => {
      setDetailOpen(true);
      setDetailLoading(true);
      try {
        const detail = await getJson<LicenseRecord>(
          `/api/tenants/${tenantId}/licenses/${lic.id}`,
        );
        setSelectedLicense(detail);
      } catch (err) {
        toast(err instanceof Error ? err.message : "Failed to load license detail", "error");
        setDetailOpen(false);
      } finally {
        setDetailLoading(false);
      }
    },
    [tenantId, toast],
  );

  /* ── Download license ────────────────────────── */
  const downloadLicense = useCallback(
    async (lic: LicenseRecord) => {
      try {
        const headers: Record<string, string> = {};
        const token = getAccessToken();
        if (token) headers.authorization = `Bearer ${token}`;
        const resp = await fetch(`/api/tenants/${tenantId}/licenses/${lic.id}/download`, {
          headers,
        });
        if (!resp.ok) throw new Error("Download failed");
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `license-${lic.fingerprint.slice(0, 8)}.lic`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
      } catch (err) {
        toast(err instanceof Error ? err.message : "Failed to download license", "error");
      }
    },
    [tenantId, toast],
  );

  /* ── Issue license ───────────────────────────── */
  const handleIssue = useCallback(async () => {
    setIssueLoading(true);
    try {
      await postJson(`/api/tenants/${tenantId}/licenses`, {
        plan: issuePlan,
        node_commit: issueNodeCommit,
        valid_months: parseInt(issueValidity, 10),
      });
      toast("License issued successfully", "success");
      setIssueOpen(false);
      setIssuePlan("observe");
      setIssueNodeCommit(10);
      setIssueValidity("12");
      void fetchLicenses();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to issue license", "error");
    } finally {
      setIssueLoading(false);
    }
  }, [tenantId, issuePlan, issueNodeCommit, issueValidity, toast, fetchLicenses]);

  /* ── Revoke license ──────────────────────────── */
  const handleRevoke = useCallback(async () => {
    if (!revokeTarget || !revokeReason.trim()) return;
    setRevokeLoading(true);
    try {
      await postJson(`/api/tenants/${tenantId}/licenses/${revokeTarget.id}/revoke`, {
        reason: revokeReason,
      });
      toast("License revoked", "success");
      setRevokeOpen(false);
      setRevokeReason("");
      setRevokeTarget(null);
      void fetchLicenses();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to revoke license", "error");
    } finally {
      setRevokeLoading(false);
    }
  }, [tenantId, revokeTarget, revokeReason, toast, fetchLicenses]);

  /* ── Validate license ────────────────────────── */
  const handleValidate = useCallback(async () => {
    if (!licenseKey.trim()) return;
    setValidateLoading(true);
    setValidateResult(null);
    try {
      const result = await postJson<ValidateResult>("/api/licenses/validate", {
        license_key: licenseKey,
      });
      setValidateResult(result);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Validation failed", "error");
    } finally {
      setValidateLoading(false);
    }
  }, [licenseKey, toast]);

  /* ── Table columns ───────────────────────────── */
  const columns = useMemo<Column<LicenseRecord>[]>(
    () => [
      {
        key: "fingerprint",
        header: "Fingerprint",
        render: (r) => (
          <code style={{ fontSize: "0.8125rem" }} title={r.fingerprint}>
            {truncate(r.fingerprint, 16)}
          </code>
        ),
      },
      {
        key: "plan",
        header: "Plan",
        render: (r) => (
          <StatusBadge
            status={r.plan}
            variant={PLAN_VARIANT[r.plan] ?? "neutral"}
            size="sm"
          />
        ),
      },
      { key: "node_commit", header: "Node Commit" },
      {
        key: "valid_from",
        header: "Valid From",
        render: (r) => formatDate(r.valid_from),
      },
      {
        key: "valid_until",
        header: "Valid Until",
        render: (r) => formatDate(r.valid_until),
      },
      {
        key: "status",
        header: "Status",
        render: (r) => (
          <StatusBadge status={r.status} variant={STATUS_VARIANT[r.status] ?? "neutral"} size="sm" />
        ),
      },
    ],
    [],
  );

  const rowActions = useMemo(
    () => [
      {
        label: "View",
        onClick: (row: LicenseRecord) => openDetail(row),
      },
      {
        label: "Download",
        onClick: (row: LicenseRecord) => void downloadLicense(row),
      },
      {
        label: "Revoke",
        variant: "danger" as const,
        onClick: (row: LicenseRecord) => {
          setRevokeTarget(row);
          setRevokeOpen(true);
        },
      },
    ],
    [openDetail, downloadLicense],
  );

  /* ── Filter handlers ─────────────────────────── */
  const handleFilterChange = useCallback((key: string, value: string) => {
    setFilters((prev) => ({ ...prev, [key]: value }));
    setPage(1);
  }, []);

  const handleFilterClear = useCallback(() => {
    setFilters({ status: "" });
    setPage(1);
  }, []);

  /* ── Render ──────────────────────────────────── */
  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />

      <PageHeader
        title="Licenses"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Operations" }, { label: "Licenses" }]}
        actions={
          <button className="btn btn-primary" onClick={() => setIssueOpen(true)}>
            Issue License
          </button>
        }
      />

      {/* Super admin: tenant selector */}
      {isPlatformAdmin && tenants.length > 0 && (
        <div style={{ marginBottom: "1rem" }}>
          <select
            className="select"
            value={tenantId}
            onChange={(e) => {
              setTenantId(e.target.value);
              setPage(1);
            }}
            aria-label="Select tenant"
            style={{ minWidth: 280 }}
          >
            {tenants.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name} ({t.slug})
              </option>
            ))}
          </select>
        </div>
      )}

      {/* Tabs */}
      <div style={tabBar}>
        <button style={tabBtn(activeTab === "licenses")} onClick={() => setActiveTab("licenses")}>
          Licenses
        </button>
        <button style={tabBtn(activeTab === "validate")} onClick={() => setActiveTab("validate")}>
          Validate
        </button>
      </div>

      {/* ── Licenses Tab ─────────────────────────── */}
      {activeTab === "licenses" && (
        <>
          <FilterBar
            filters={filterDefs}
            values={filters}
            onChange={handleFilterChange}
            onClear={handleFilterClear}
          />

          {!loading && licenses.length === 0 && !filters.status ? (
            <EmptyState
              title="No licenses issued"
              description="Issue your first license to get started."
              actionLabel="Issue License"
              onAction={() => setIssueOpen(true)}
              icon="&#x26BF;"
            />
          ) : (
            <DataTable<LicenseRecord>
              columns={columns}
              data={licenses}
              loading={loading}
              emptyMessage="No licenses match your filters"
              page={page}
              pageSize={PAGE_SIZE}
              total={total}
              onPageChange={setPage}
              onRowClick={openDetail}
              rowActions={rowActions}
              striped
            />
          )}
        </>
      )}

      {/* ── Validate Tab ─────────────────────────── */}
      {activeTab === "validate" && (
        <div className="card" style={{ padding: "1.5rem" }}>
          <div style={formGroup}>
            <label style={labelStyle}>License Key</label>
            <textarea
              className="input"
              value={licenseKey}
              onChange={(e) => setLicenseKey(e.target.value)}
              rows={6}
              placeholder="Paste the license key here..."
              style={{ width: "100%", resize: "vertical", fontFamily: "monospace", fontSize: "0.8125rem" }}
            />
          </div>
          <button
            className="btn btn-primary"
            onClick={handleValidate}
            disabled={validateLoading || !licenseKey.trim()}
          >
            {validateLoading && <Spinner size="sm" />}
            Validate
          </button>

          {validateResult && (
            <div style={validateResultBox}>
              <div style={{ marginBottom: "0.75rem" }}>
                <StatusBadge
                  status={validateResult.valid ? "Valid" : "Invalid"}
                  variant={validateResult.valid ? "success" : "critical"}
                />
              </div>
              {validateResult.valid && validateResult.payload && (
                <>
                  <div style={detailRow}>
                    <span style={detailLabel}>Tenant</span>
                    <span style={detailValue}>{validateResult.payload.tenant}</span>
                  </div>
                  <div style={detailRow}>
                    <span style={detailLabel}>Plan</span>
                    <span style={detailValue}>{validateResult.payload.plan}</span>
                  </div>
                  <div style={detailRow}>
                    <span style={detailLabel}>Node Commit</span>
                    <span style={detailValue}>{validateResult.payload.node_commit}</span>
                  </div>
                  <div style={detailRow}>
                    <span style={detailLabel}>Expires</span>
                    <span style={detailValue}>{formatDate(validateResult.payload.expires_at)}</span>
                  </div>
                </>
              )}
              {validateResult.errors && validateResult.errors.length > 0 && (
                <div style={{ marginTop: "0.75rem" }}>
                  <div style={{ fontSize: "0.8125rem", fontWeight: 600, color: "var(--status-critical)", marginBottom: "0.375rem" }}>
                    Errors:
                  </div>
                  <ul style={{ margin: 0, paddingLeft: "1.25rem", fontSize: "0.8125rem", color: "var(--status-critical)" }}>
                    {validateResult.errors.map((err, i) => (
                      <li key={i}>{err}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* ── Detail Panel ───────────────────────── */}
      <DetailPanel
        open={detailOpen}
        onClose={() => {
          setDetailOpen(false);
          setSelectedLicense(null);
        }}
        title="License Details"
        subtitle={selectedLicense ? truncate(selectedLicense.fingerprint, 24) : undefined}
        width={500}
      >
        {detailLoading ? (
          <div style={{ display: "flex", justifyContent: "center", padding: "2rem" }}>
            <Spinner size="md" />
          </div>
        ) : selectedLicense ? (
          <>
            <div style={sectionTitle}>License Info</div>
            <div style={detailRow}>
              <span style={detailLabel}>Fingerprint</span>
              <span style={{ ...detailValue, ...monoStyle }}>{selectedLicense.fingerprint}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Plan</span>
              <span style={detailValue}>
                <StatusBadge
                  status={selectedLicense.plan}
                  variant={PLAN_VARIANT[selectedLicense.plan] ?? "neutral"}
                  size="sm"
                />
              </span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Node Commit</span>
              <span style={detailValue}>{selectedLicense.node_commit}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Valid From</span>
              <span style={detailValue}>{formatDate(selectedLicense.valid_from)}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Valid Until</span>
              <span style={detailValue}>{formatDate(selectedLicense.valid_until)}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Status</span>
              <span style={detailValue}>
                <StatusBadge
                  status={selectedLicense.status}
                  variant={STATUS_VARIANT[selectedLicense.status] ?? "neutral"}
                  size="sm"
                />
              </span>
            </div>

            <div style={sectionTitle}>Issuance</div>
            <div style={detailRow}>
              <span style={detailLabel}>Issued By</span>
              <span style={detailValue}>{selectedLicense.issued_by}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Issued At</span>
              <span style={detailValue}>{formatDate(selectedLicense.issued_at)}</span>
            </div>

            {selectedLicense.status === "revoked" && (
              <>
                <div style={sectionTitle}>Revocation</div>
                <div style={detailRow}>
                  <span style={detailLabel}>Revoked By</span>
                  <span style={detailValue}>{selectedLicense.revoked_by ?? "--"}</span>
                </div>
                <div style={detailRow}>
                  <span style={detailLabel}>Revoked At</span>
                  <span style={detailValue}>{formatDate(selectedLicense.revoked_at ?? "")}</span>
                </div>
                <div style={detailRow}>
                  <span style={detailLabel}>Reason</span>
                  <span style={detailValue}>{selectedLicense.revoke_reason ?? "--"}</span>
                </div>
              </>
            )}

            {selectedLicense.status === "active" && (
              <div style={{ marginTop: "1.5rem", display: "flex", gap: "0.5rem" }}>
                <button
                  className="btn"
                  onClick={() => void downloadLicense(selectedLicense)}
                >
                  Download .lic
                </button>
                <button
                  className="btn btn-danger"
                  onClick={() => {
                    setRevokeTarget(selectedLicense);
                    setRevokeOpen(true);
                  }}
                >
                  Revoke
                </button>
              </div>
            )}
          </>
        ) : null}
      </DetailPanel>

      {/* ── Revoke Confirm ─────────────────────── */}
      <ConfirmDialog
        open={revokeOpen}
        title="Revoke License"
        message={
          revokeReason.trim()
            ? `Revoke license "${truncate(revokeTarget?.fingerprint ?? "", 16)}"? This cannot be undone.`
            : `Revoke license "${truncate(revokeTarget?.fingerprint ?? "", 16)}"? Please provide a reason.`
        }
        confirmLabel="Revoke"
        variant="danger"
        onConfirm={handleRevoke}
        onCancel={() => {
          setRevokeOpen(false);
          setRevokeReason("");
          setRevokeTarget(null);
        }}
        loading={revokeLoading}
      />
      {revokeOpen && (
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
            <label style={labelStyle}>Reason for revocation</label>
            <textarea
              className="input"
              value={revokeReason}
              onChange={(e) => setRevokeReason(e.target.value)}
              rows={3}
              placeholder="Explain why this license is being revoked..."
              style={{ width: "100%", resize: "vertical" }}
            />
          </div>
        </div>
      )}

      {/* ── Issue License Modal ────────────────── */}
      {issueOpen && (
        <div style={modalOverlay} onClick={() => setIssueOpen(false)} role="presentation">
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="issue-license-title"
            style={modalCard}
            onClick={(e) => e.stopPropagation()}
          >
            <h3 id="issue-license-title" style={{ margin: "0 0 1rem 0", fontSize: "1.125rem", fontWeight: 600 }}>
              Issue License
            </h3>

            <div style={formGroup}>
              <label style={labelStyle}>Plan</label>
              <select
                className="select"
                value={issuePlan}
                onChange={(e) => setIssuePlan(e.target.value)}
                style={{ width: "100%" }}
              >
                {PLAN_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </div>

            <div style={formGroup}>
              <label style={labelStyle}>Node Commit</label>
              <input
                className="input"
                type="number"
                min={1}
                value={issueNodeCommit}
                onChange={(e) => setIssueNodeCommit(parseInt(e.target.value, 10) || 0)}
                style={{ width: "100%" }}
              />
            </div>

            <div style={formGroup}>
              <label style={labelStyle}>Validity</label>
              <select
                className="select"
                value={issueValidity}
                onChange={(e) => setIssueValidity(e.target.value)}
                style={{ width: "100%" }}
              >
                {VALIDITY_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </div>

            <div style={{ display: "flex", justifyContent: "flex-end", gap: "0.5rem", marginTop: "1.25rem" }}>
              <button className="btn" onClick={() => setIssueOpen(false)} disabled={issueLoading}>
                Cancel
              </button>
              <button className="btn btn-primary" onClick={handleIssue} disabled={issueLoading}>
                {issueLoading && <Spinner size="sm" />}
                Issue License
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
