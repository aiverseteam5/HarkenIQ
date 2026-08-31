import { useParams } from "react-router-dom";
import { type CSSProperties, useCallback, useEffect, useState } from "react";
import PageHeader from "../components/PageHeader";
import DataTable, { type Column } from "../components/DataTable";
import DetailPanel from "../components/DetailPanel";
import MetricCard from "../components/MetricCard";
import StatusBadge from "../components/StatusBadge";
import EmptyState from "../components/EmptyState";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";
import { getJson } from "../api";

/* Capability Registry — what this fleet's executors can ACTUALLY do.
 *
 * This page is a CONSUMER of GET /api/capabilities. It has no capability
 * model of its own and must never grow one: the node declares, the Site
 * Manager stores, Central Command composes, this renders. Nothing here
 * decides whether a class is implemented, reachable, risky or
 * reversible — every one of those is read from the contract.
 *
 * Three things the page must never blur:
 *   1. Capability is not permission, scope, autonomy, approval, or
 *      execution authority. "Available" means an executor exists, not
 *      that anyone may run it. The node's allow list is still the final
 *      execution authority.
 *   2. Unknown is not zero. A device that has not declared may well be
 *      capable; showing it as incapable would be a lie about every
 *      fleet mid-upgrade.
 *   3. "No code for it" and "not permitted on this node" are different
 *      problems with different fixes, and stay visibly different. */

interface Blocked {
  reason: string;
  device_count: number;
}

interface CapabilityClass {
  action_type: string;
  risk: string;
  reversibility: string;
  inverse_action: string | null;
  implemented: boolean;
  implemented_by: string[];
  reach: string;
  effective_device_count: number;
  undeclared_device_count: number;
  devices_in_view: number;
  effective_sites: { id: string; name: string }[];
  effective_devices: {
    agent_id: string;
    agent_name: string;
    site_id: string;
    device_class: string;
    protocol: string | null;
  }[];
  effective_devices_truncated: boolean;
  blocked_by: Blocked[];
  reason: string;
}

interface Registry {
  tenant_id: string;
  fleet: {
    devices_in_view: number;
    declared: number;
    undeclared: number;
    protocols: string[];
  };
  classes: CapabilityClass[];
  contract: { authority: string; unknown: string };
}

const REACH_LABEL: Record<string, string> = {
  available: "Available",
  unimplemented: "Not implemented",
  no_effective_reach: "No device can run it",
  unknown: "Unknown",
};

const REACH_VARIANT: Record<
  string,
  "success" | "warning" | "critical" | "info" | "neutral"
> = {
  available: "success",
  unimplemented: "critical",
  no_effective_reach: "warning",
  unknown: "neutral",
};

const RISK_VARIANT: Record<string, "success" | "warning" | "critical" | "neutral"> = {
  none: "neutral",
  low: "success",
  medium: "warning",
  high: "critical",
};

/* Plain language for the contract's codes. An operator should never have
   to read an enum to learn why something cannot run. */
const BLOCK_LABEL: Record<string, string> = {
  no_executor_implements_it: "No executor on this platform implements it",
  device_protocol_does_not_implement_it:
    "The device's protocol does not implement it",
  not_on_this_node_allow_list: "Not permitted on the node's allow list",
  device_has_not_declared: "The device has not declared its capabilities",
};

const REVERSIBILITY_LABEL: Record<string, string> = {
  none: "Changes no device state",
  self_reverting: "The device returns to its prior state on its own",
  reversible: "A governed action restores the prior state",
  irreversible: "Destroys state that nothing can restore",
};

const metricsRow: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))",
  gap: "1rem",
  marginBottom: "1.5rem",
};

const helpText: CSSProperties = {
  fontSize: "0.8125rem",
  color: "var(--text-secondary)",
  marginBottom: "1rem",
  maxWidth: "72ch",
  lineHeight: 1.6,
};

const bannerBase: CSSProperties = {
  padding: "0.75rem 1rem",
  borderRadius: "var(--radius-md)",
  fontSize: "0.8125rem",
  marginBottom: "1rem",
  display: "flex",
  alignItems: "center",
  gap: "0.75rem",
  flexWrap: "wrap",
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
  gap: "1rem",
  padding: "0.375rem 0",
  fontSize: "0.8125rem",
  borderBottom: "1px solid var(--border-light)",
};
const detailLabel: CSSProperties = { color: "var(--text-secondary)", fontWeight: 500 };
const detailValue: CSSProperties = {
  color: "var(--text-primary)",
  fontWeight: 500,
  textAlign: "right",
};

export default function Capabilities() {
  const { tenantId = "" } = useParams<{ tenantId: string }>();
  const { toasts, toast, dismiss } = useToast();

  const [registry, setRegistry] = useState<Registry | null>(null);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<CapabilityClass | null>(null);

  const fetchAll = useCallback(async () => {
    try {
      setRegistry(await getJson<Registry>(`/api/t/${tenantId}/capabilities/`));
    } catch (err) {
      toast(
        err instanceof Error ? err.message : "Failed to load the capability registry",
        "error",
      );
    } finally {
      setLoading(false);
    }
  }, [tenantId, toast]);

  useEffect(() => {
    setLoading(true);
    void fetchAll();
  }, [fetchAll]);

  const columns: Column<CapabilityClass>[] = [
    {
      key: "action_type",
      header: "Action class",
      render: (r) => (
        <span style={{ fontSize: "0.8125rem", fontWeight: 600 }}>
          {r.action_type.replace(/_/g, " ")}
          <div style={{ fontSize: "0.72rem", color: "var(--text-muted)", fontWeight: 400 }}>
            {r.implemented
              ? `implemented by ${r.implemented_by.join(", ")}`
              : "no executor implements it"}
          </div>
        </span>
      ),
    },
    {
      key: "risk",
      header: "Risk",
      render: (r) => (
        <StatusBadge status={r.risk} variant={RISK_VARIANT[r.risk] ?? "neutral"} size="sm" />
      ),
    },
    {
      key: "reversibility",
      header: "Reversibility",
      render: (r) => (
        <StatusBadge
          status={r.reversibility.replace(/_/g, " ")}
          variant={r.reversibility === "irreversible" ? "critical" : "neutral"}
          size="sm"
        />
      ),
    },
    {
      key: "reach",
      header: "Can it run",
      render: (r) => (
        <StatusBadge
          status={REACH_LABEL[r.reach] ?? r.reach}
          variant={REACH_VARIANT[r.reach] ?? "neutral"}
          size="sm"
        />
      ),
    },
    {
      key: "effective_device_count",
      header: "Devices that can",
      render: (r) => (
        <span style={{ fontSize: "0.8125rem" }}>
          {r.effective_device_count}
          <span style={{ color: "var(--text-muted)" }}> / {r.devices_in_view}</span>
          {r.undeclared_device_count > 0 && (
            <div style={{ fontSize: "0.72rem", color: "var(--text-muted)" }}>
              {r.undeclared_device_count} undeclared
            </div>
          )}
        </span>
      ),
    },
  ];

  const classes = registry?.classes ?? [];
  const unimplemented = classes.filter((c) => !c.implemented);
  const available = classes.filter((c) => c.reach === "available").length;
  const fleet = registry?.fleet;

  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />
      <PageHeader
        title="Capabilities"
        breadcrumbs={[
          { label: "HarkenIQ" },
          { label: "Governance" },
          { label: "Capabilities" },
        ]}
      />

      <p style={helpText}>
        What the executors on this fleet can actually do, reflected from each node's own
        declaration. This is not permission and not autonomy: a class shown as available
        still passes every gate on the device, and the node's allow list remains the final
        execution authority. A device that has not declared reads as unknown — never as
        incapable.
      </p>

      {unimplemented.length > 0 && (
        <div
          style={{
            ...bannerBase,
            background: "var(--status-critical-bg)",
            color: "var(--status-critical)",
            border: "1px solid var(--status-critical)",
          }}
        >
          <strong>
            {unimplemented.length} governed action class
            {unimplemented.length === 1 ? "" : "es"} has no executor.
          </strong>
          <span>
            {unimplemented.map((c) => c.action_type.replace(/_/g, " ")).join(", ")} — fully
            governed, with risk, preconditions and blast radius intact, and no
            implementation behind them. No device can run them and no agent may be bound to
            them.
          </span>
        </div>
      )}

      {fleet != null && fleet.undeclared > 0 && (
        <div
          style={{
            ...bannerBase,
            background: "var(--status-neutral-bg)",
            color: "var(--text-secondary)",
            border: "1px solid var(--border-color)",
          }}
        >
          <strong>
            {fleet.undeclared} device{fleet.undeclared === 1 ? "" : "s"} has not declared.
          </strong>
          <span>
            Their reach is unknown, not zero. Declarations arrive as agents re-register.
          </span>
        </div>
      )}

      <div style={metricsRow}>
        <MetricCard title="Classes available on this fleet" value={available} />
        <MetricCard title="Classes with no executor" value={unimplemented.length} />
        <MetricCard title="Devices declared" value={fleet?.declared ?? "--"} />
        <MetricCard
          title="Protocols in use"
          value={fleet?.protocols.length ? fleet.protocols.join(", ") : "--"}
        />
      </div>

      <h2 style={sectionTitle}>Action classes</h2>
      {classes.length === 0 && !loading ? (
        <EmptyState
          title="No action classes"
          description="The capability registry returned no classes for this tenant."
          icon="&#x1F527;"
        />
      ) : (
        <DataTable<CapabilityClass>
          columns={columns}
          data={classes}
          loading={loading}
          emptyMessage="No action classes"
          onRowClick={(r) => setSelected(r)}
          striped
        />
      )}

      <DetailPanel
        open={selected !== null}
        onClose={() => setSelected(null)}
        title={selected?.action_type.replace(/_/g, " ") ?? ""}
        subtitle={selected ? REACH_LABEL[selected.reach] ?? selected.reach : ""}
      >
        {selected && (
          <div>
            <div style={sectionTitle}>Why</div>
            <p style={{ fontSize: "0.8125rem", lineHeight: 1.6 }}>{selected.reason}</p>

            <div style={sectionTitle}>The action class</div>
            <div style={detailRow}>
              <span style={detailLabel}>Risk</span>
              <span style={detailValue}>{selected.risk}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Reversibility</span>
              <span style={detailValue}>
                {REVERSIBILITY_LABEL[selected.reversibility] ?? selected.reversibility}
              </span>
            </div>
            {selected.inverse_action && (
              <div style={detailRow}>
                <span style={detailLabel}>Reversed by</span>
                <span style={detailValue}>
                  {selected.inverse_action.replace(/_/g, " ")}
                </span>
              </div>
            )}
            <div style={detailRow}>
              <span style={detailLabel}>Implemented by</span>
              <span style={detailValue}>
                {selected.implemented_by.length > 0
                  ? selected.implemented_by.join(", ")
                  : "nothing"}
              </span>
            </div>

            {selected.blocked_by.length > 0 && (
              <>
                <div style={sectionTitle}>What is blocking it</div>
                {selected.blocked_by.map((b) => (
                  <div key={b.reason} style={detailRow}>
                    <span style={detailLabel}>{BLOCK_LABEL[b.reason] ?? b.reason}</span>
                    <span style={detailValue}>
                      {b.device_count} device{b.device_count === 1 ? "" : "s"}
                    </span>
                  </div>
                ))}
              </>
            )}

            {selected.effective_devices.length > 0 && (
              <>
                <div style={sectionTitle}>Devices that can run it</div>
                {selected.effective_devices.map((d) => (
                  <div key={d.agent_id} style={detailRow}>
                    <span style={detailLabel}>{d.agent_name || d.agent_id}</span>
                    <span style={detailValue}>
                      {d.device_class}
                      {d.protocol ? ` · ${d.protocol}` : ""}
                    </span>
                  </div>
                ))}
                {selected.effective_devices_truncated && (
                  <p style={{ fontSize: "0.78rem", color: "var(--text-muted)" }}>
                    Showing the first {selected.effective_devices.length} of{" "}
                    {selected.effective_device_count}.
                  </p>
                )}
              </>
            )}

            {selected.effective_sites.length > 0 && (
              <>
                <div style={sectionTitle}>Sites with reach</div>
                {selected.effective_sites.map((s) => (
                  <div key={s.id} style={detailRow}>
                    <span style={detailLabel}>{s.name || s.id}</span>
                  </div>
                ))}
              </>
            )}

            <div style={sectionTitle}>What this does not mean</div>
            <p style={{ fontSize: "0.8125rem", lineHeight: 1.6 }}>
              {registry?.contract.authority}
            </p>
          </div>
        )}
      </DetailPanel>
    </div>
  );
}
