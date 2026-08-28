import { useCallback, useEffect, useState } from "react";
import PageHeader from "../components/PageHeader";
import DataTable from "../components/DataTable";
import EmptyState from "../components/EmptyState";
import Spinner from "../components/Spinner";
import { useToast } from "../components/useToast";
import Toast from "../components/Toast";
import { getJson, postJson } from "../api";

/**
 * The approver's queue for support access into customer tenants.
 *
 * Support raises a request and cannot approve it — that separation is the
 * whole point, so this page is reachable only by platform_super_admin and
 * the endpoints behind it enforce the same.
 */

interface AccessRequest {
  id: string;
  tenant_id: string;
  status: string;
  requested_by: string;
  requested_at: string | null;
  reason: string | null;
}

interface TenantRow {
  id: string;
  name: string;
}

export default function SupportAccessRequests() {
  const { toasts, toast, dismiss } = useToast();
  const [items, setItems] = useState<AccessRequest[]>([]);
  const [tenantNames, setTenantNames] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  // A failed fetch must not render as the success-shaped "queue is clear"
  // — an approver who missed the toast would read a false all-clear on an
  // access-control page (review, design pass).
  const [failed, setFailed] = useState(false);
  const [acting, setActing] = useState<string | null>(null);

  const fetchPending = useCallback(async () => {
    setLoading(true);
    try {
      const res = await getJson<{ items: AccessRequest[] }>(
        "/api/admin/support-access/requests/pending",
      );
      setItems(res.items ?? []);
      setFailed(false);
      // Approvers decide who enters WHICH CUSTOMER — a raw hex id is not
      // an answer. The caller is a super admin, who may read the registry.
      try {
        const tenants = await getJson<{ items: TenantRow[] }>(
          "/api/admin/tenants/?page_size=200",
        );
        setTenantNames(
          Object.fromEntries((tenants.items ?? []).map((t) => [t.id, t.name])),
        );
      } catch {
        /* names are an enhancement; ids still render */
      }
    } catch (e) {
      setFailed(true);
      toast((e as Error).message, "error");
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    void fetchPending();
  }, [fetchPending]);

  const decide = useCallback(
    async (id: string, decision: "approve" | "deny") => {
      setActing(id);
      try {
        const res = await postJson<{ access?: { expires_at?: string } }>(
          `/api/admin/support-access/requests/${id}/${decision}`,
          {},
        );
        // Duration derives from the server's expires_at rather than
        // restating the TTL (review: "24" lived in five places).
        const exp = res.access?.expires_at;
        const hours = exp
          ? Math.round((new Date(exp).getTime() - Date.now()) / 3.6e6)
          : null;
        toast(
          decision === "approve"
            ? `Access granted${hours ? ` for ~${hours}h` : ""}`
            : "Request denied",
          "success",
        );
        await fetchPending();
      } catch (e) {
        toast((e as Error).message, "error");
      } finally {
        setActing(null);
      }
    },
    [toast, fetchPending],
  );

  return (
    <>
      <PageHeader
        title="Support Access"
        breadcrumbs={[
          { label: "Platform Console" },
          { label: "Support Access" },
        ]}
      />

      {loading ? (
        <Spinner />
      ) : failed ? (
        <EmptyState
          title="Could not load the request queue"
          description="The pending-requests fetch failed, so this page cannot say whether the queue is clear."
          actionLabel="Retry"
          onAction={() => void fetchPending()}
        />
      ) : items.length === 0 ? (
        <EmptyState
          title="No pending requests"
          description="Support access is time-bound and expires on its own; approved grants do not appear here."
        />
      ) : (
        <DataTable<AccessRequest>
          columns={[
            {
              key: "tenant_id",
              header: "Tenant",
              render: (r) => tenantNames[r.tenant_id] ?? r.tenant_id,
            },
            { key: "requested_by", header: "Requested by" },
            {
              key: "requested_at",
              header: "Requested",
              render: (r) =>
                r.requested_at
                  ? new Date(r.requested_at).toLocaleString()
                  : "—",
            },
            {
              key: "reason",
              header: "Reason",
              render: (r) => r.reason || "—",
            },
            {
              key: "actions",
              header: "",
              render: (r) => (
                <span style={{ display: "flex", gap: "0.5rem" }}>
                  <button
                    className="btn btn-primary btn-sm"
                    disabled={acting === r.id}
                    onClick={() => void decide(r.id, "approve")}
                  >
                    Approve
                  </button>
                  <button
                    className="btn btn-danger btn-sm"
                    disabled={acting === r.id}
                    onClick={() => void decide(r.id, "deny")}
                  >
                    Deny
                  </button>
                </span>
              ),
            },
          ]}
          data={items}
        />
      )}
      <Toast toasts={toasts} onDismiss={dismiss} />
    </>
  );
}
