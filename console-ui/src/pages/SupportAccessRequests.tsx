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

export default function SupportAccessRequests() {
  const { toasts, toast, dismiss } = useToast();
  const [items, setItems] = useState<AccessRequest[]>([]);
  const [loading, setLoading] = useState(true);
  const [acting, setActing] = useState<string | null>(null);

  const fetchPending = useCallback(async () => {
    setLoading(true);
    try {
      const res = await getJson<{ items: AccessRequest[] }>(
        "/api/admin/support-access/requests/pending",
      );
      setItems(res.items ?? []);
    } catch (e) {
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
        await postJson(
          `/api/admin/support-access/requests/${id}/${decision}`,
          {},
        );
        toast(
          decision === "approve"
            ? "Access granted for 24 hours"
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
      ) : items.length === 0 ? (
        <EmptyState
          title="No pending requests"
          description="Support access is time-bound and expires on its own; approved grants do not appear here."
        />
      ) : (
        <DataTable<AccessRequest>
          columns={[
            { key: "tenant_id", header: "Tenant" },
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
                    className="btn btn-sm"
                    disabled={acting === r.id}
                    onClick={() => void decide(r.id, "approve")}
                  >
                    Approve 24h
                  </button>
                  <button
                    className="btn btn-sm"
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
