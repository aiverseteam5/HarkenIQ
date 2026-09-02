import { useParams } from "react-router-dom";
import { type CSSProperties, useCallback, useEffect, useMemo, useState } from "react";
import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";
import { deleteJson, getJson, postJson, putJson } from "../api";
import { useAuth } from "../useAuth";

/* E1.2 — Access Scope: who may reach what.
 *
 * A grant is (principal, permission subset, scope refs). It is the ONLY
 * thing that confers authority: the organizational tree says where a
 * site sits and grants nobody anything.
 *
 * Two rules this page surfaces but does not enforce — Central Command
 * enforces both, and every control here would 403 without it:
 *
 *   - a subset can only NARROW the role, never widen it
 *   - a grantor can only hand out scope they themselves hold
 *
 * Nothing here is a security boundary. Hiding a button is not
 * authorization; the server refuses, and this page renders the refusal. */

/* ── Types ────────────────────────────────────────── */

interface Grant {
  id: string;
  principal_type: string;
  principal_ref: string;
  scope_type: string;
  scope_ref: string;
  permission_subset: string[] | null;
  role: string;
  granted_by: string;
  granted_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
  note: string;
  // A23-3: whether the target still exists, and whether the row confers
  // anything right now. Rendered, never recomputed here.
  target_status?: string;
  effective?: boolean;
}

interface GrantsResponse {
  grants: Grant[];
  scope_types: string[];
  principal_types: string[];
  enforcement: string;
}

interface Enforcement {
  scope_enforcement: string;
  modes: string[];
  strict_ready: boolean;
  strict_blocked_reason: string;
  tenant_admin_count: number;
}

interface MyScope {
  tenant_wide: boolean;
  site_ids: string[];
  org_unit_paths: string[];
  contextual_unit_ids: { ids: string[]; authority: boolean; note: string };
  grants: { scope_type: string; scope_ref: string; permissions: string[] }[];
  // A23.9: grants whose target vanished. Retained, reach none, reason stated.
  inert_grants?: { scope_type: string; scope_ref: string; reason: string }[];
  administered?: boolean;
}

interface UnitNode {
  id: string;
  name: string;
  depth: number;
  children: UnitNode[];
}

interface SiteRow {
  id: string;
  site_name: string;
}

/* ── Styles ───────────────────────────────────────── */

const card: CSSProperties = {
  background: "var(--surface)",
  border: "1px solid var(--border)",
  borderRadius: "8px",
  padding: "1rem 1.125rem",
  marginBottom: "1.25rem",
};

const noteStyle: CSSProperties = {
  ...card,
  borderLeft: "3px solid var(--accent, #3b82f6)",
  fontSize: "0.8125rem",
  color: "var(--text-muted)",
  lineHeight: 1.55,
};

const labelStyle: CSSProperties = {
  display: "block",
  fontSize: "0.75rem",
  color: "var(--text-muted)",
  marginBottom: "0.25rem",
};

const inputStyle: CSSProperties = {
  width: "100%",
  padding: "0.375rem 0.5rem",
  fontSize: "0.8125rem",
  borderRadius: "6px",
  border: "1px solid var(--border)",
  background: "var(--surface)",
  color: "var(--text)",
  marginBottom: "0.625rem",
};

const button = (kind: "primary" | "quiet" | "danger"): CSSProperties => ({
  padding: "0.375rem 0.75rem",
  fontSize: "0.8125rem",
  borderRadius: "6px",
  cursor: "pointer",
  border: "1px solid var(--border)",
  background: kind === "primary" ? "var(--accent, #3b82f6)" : "transparent",
  color:
    kind === "primary"
      ? "#fff"
      : kind === "danger"
        ? "var(--danger, #ef4444)"
        : "var(--text)",
});

const chip = (tone: "ok" | "warn" | "mute"): CSSProperties => ({
  display: "inline-block",
  fontSize: "0.6875rem",
  letterSpacing: "0.03em",
  padding: "0 0.375rem",
  borderRadius: "4px",
  border: "1px solid var(--border)",
  color:
    tone === "ok"
      ? "var(--success, #16a34a)"
      : tone === "warn"
        ? "var(--danger, #ef4444)"
        : "var(--text-muted)",
  whiteSpace: "nowrap",
});

const tableStyle: CSSProperties = {
  width: "100%",
  borderCollapse: "collapse",
  fontSize: "0.8125rem",
};

const thStyle: CSSProperties = {
  textAlign: "left",
  padding: "0.375rem 0.5rem",
  fontSize: "0.6875rem",
  textTransform: "uppercase",
  letterSpacing: "0.05em",
  color: "var(--text-muted)",
  borderBottom: "1px solid var(--border)",
};

const tdStyle: CSSProperties = {
  padding: "0.4375rem 0.5rem",
  borderBottom: "1px solid var(--border)",
  verticalAlign: "top",
};

const monoStyle: CSSProperties = {
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
  fontSize: "0.6875rem",
  color: "var(--text-muted)",
  wordBreak: "break-all",
};

/* ── Helpers ──────────────────────────────────────── */

function flatUnits(nodes: UnitNode[]): UnitNode[] {
  return nodes.flatMap((n) => [n, ...flatUnits(n.children)]);
}

/* ── Page ─────────────────────────────────────────── */

export default function AccessScope() {
  const { tenantId = "" } = useParams<{ tenantId: string }>();
  const { user } = useAuth();
  const { toasts, toast, dismiss } = useToast();

  const [data, setData] = useState<GrantsResponse | null>(null);
  const [enforcement, setEnforcement] = useState<Enforcement | null>(null);
  const [mine, setMine] = useState<MyScope | null>(null);
  const [units, setUnits] = useState<UnitNode[]>([]);
  const [sites, setSites] = useState<SiteRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [showRevoked, setShowRevoked] = useState(false);

  const [principal, setPrincipal] = useState("");
  const [role, setRole] = useState("site_admin");
  const [scopeType, setScopeType] = useState("org_unit");
  const [scopeRef, setScopeRef] = useState("");
  const [note, setNote] = useState("");

  const perms = user?.permissions ?? [];
  const canManage = perms.includes("*") || perms.includes("role.manage");

  const load = useCallback(async () => {
    try {
      const [g, e, m] = await Promise.all([
        getJson<GrantsResponse>(
          `/api/t/${tenantId}/scope-grants/?include_revoked=${showRevoked}`,
        ),
        getJson<Enforcement>(`/api/t/${tenantId}/tenant-settings/scope-enforcement`),
        getJson<MyScope>(`/api/t/${tenantId}/scope-grants/me`),
      ]);
      setData(g);
      setEnforcement(e);
      setMine(m);
      // Best effort: a principal without site.view still administers grants.
      try {
        const t = await getJson<{ tree: UnitNode[] }>(
          `/api/t/${tenantId}/org-units/`,
        );
        setUnits(flatUnits(t.tree));
      } catch {
        setUnits([]);
      }
      try {
        const s = await getJson<{ sites: SiteRow[] } | SiteRow[]>(
          `/api/t/${tenantId}/sites/`,
        );
        setSites(Array.isArray(s) ? s : s.sites);
      } catch {
        setSites([]);
      }
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setLoading(false);
    }
  }, [tenantId, toast, showRevoked]);

  useEffect(() => {
    void load();
  }, [load]);

  const refOptions = useMemo(() => {
    if (scopeType === "org_unit")
      return units.map((u) => ({
        value: u.id,
        label: `${"— ".repeat(Math.max(0, u.depth - 1))}${u.name}`,
      }));
    if (scopeType === "site")
      return sites.map((s) => ({ value: s.id, label: s.site_name }));
    if (scopeType === "device_class")
      return [
        { value: "server", label: "server" },
        { value: "switch", label: "switch" },
      ];
    return [];
  }, [scopeType, units, sites]);

  async function run(work: () => Promise<unknown>, ok: string) {
    try {
      await work();
      toast(ok, "success");
      await load();
    } catch (err) {
      // Every refusal below is the server's, rendered verbatim: outside
      // your scope, a subset that would widen a role, the last-admin
      // preflight.
      toast((err as Error).message, "error");
    }
  }

  const grant = () =>
    run(async () => {
      await postJson(`/api/t/${tenantId}/scope-grants/`, {
        principal_ref: principal,
        principal_type: "user",
        scope_type: scopeType,
        scope_ref: scopeType === "tenant" ? "" : scopeRef,
        role,
        note,
      });
      setPrincipal("");
      setNote("");
    }, "Scope granted");

  const revoke = (id: string) =>
    run(() => deleteJson(`/api/t/${tenantId}/scope-grants/${id}`), "Grant revoked");

  // A23-3: the safe path off an org unit about to be deleted. Revoke +
  // grant in one server transaction, gated on both targets there.
  const [reassign, setReassign] = useState<{
    id: string;
    scopeType: string;
    scopeRef: string;
  } | null>(null);
  const submitReassign = () =>
    reassign &&
    run(async () => {
      await postJson(`/api/t/${tenantId}/scope-grants/${reassign.id}/reassign`, {
        scope_type: reassign.scopeType,
        scope_ref: reassign.scopeType === "tenant" ? "" : reassign.scopeRef,
      });
      setReassign(null);
    }, "Grant reassigned");

  const flip = (mode: string) =>
    run(
      () =>
        putJson(`/api/t/${tenantId}/tenant-settings/scope-enforcement`, { mode }),
      `Enforcement set to ${mode}`,
    );

  if (loading) return <div style={{ padding: "1rem" }}>Loading…</div>;

  const strict = enforcement?.scope_enforcement === "strict";

  return (
    <div>
      <PageHeader
        title="Access Scope"
        breadcrumbs={[{ label: "Tenant" }, { label: "Access Scope" }]}
      />

      <div style={noteStyle}>
        <strong>A grant is the only thing that confers authority.</strong> The
        organizational tree describes where a site sits; a grant here says who
        may reach it. A permission subset can only narrow the role it names, and
        you can only hand out scope you hold yourself — both enforced at Central
        Command, so every control on this page is refused there rather than
        hidden here.
      </div>

      <div style={card}>
        <div style={{ fontSize: "0.9375rem", fontWeight: 600, marginBottom: "0.5rem" }}>
          Enforcement
        </div>
        <div style={{ fontSize: "0.8125rem", marginBottom: "0.625rem" }}>
          This tenant is{" "}
          <span style={chip(strict ? "ok" : "mute")}>
            {enforcement?.scope_enforcement}
          </span>{" "}
          {strict ? (
            <>— a principal with no grant reaches nothing.</>
          ) : (
            <>
              — a principal with no grant still reaches the whole tenant, which
              is the pre-E1.2 behaviour. Grants are honoured for anyone who has
              one, so you can adopt scoping a person at a time.
            </>
          )}
        </div>
        {!strict && enforcement && !enforcement.strict_ready ? (
          <div
            style={{
              fontSize: "0.8125rem",
              color: "var(--danger, #ef4444)",
              marginBottom: "0.625rem",
            }}
          >
            Not ready for strict: {enforcement.strict_blocked_reason}
          </div>
        ) : null}
        {canManage ? (
          <button
            style={button("quiet")}
            onClick={() => flip(strict ? "legacy_open" : "strict")}
          >
            {strict ? "Return to legacy_open" : "Switch to strict"}
          </button>
        ) : null}
      </div>

      {mine ? (
        <div style={card}>
          <div
            style={{ fontSize: "0.9375rem", fontWeight: 600, marginBottom: "0.5rem" }}
          >
            Your own reach
          </div>
          <div style={{ fontSize: "0.8125rem" }}>
            {mine.tenant_wide
              ? "Tenant-wide."
              : `${mine.site_ids.length} site(s), ${mine.org_unit_paths.length} organizational unit(s).`}
          </div>
          {mine.contextual_unit_ids.ids.length > 0 ? (
            <div
              style={{
                fontSize: "0.75rem",
                color: "var(--text-muted)",
                marginTop: "0.375rem",
              }}
            >
              {mine.contextual_unit_ids.ids.length} ancestor unit(s) visible for
              navigation. {mine.contextual_unit_ids.note}.
            </div>
          ) : null}
          {mine.inert_grants && mine.inert_grants.length > 0 ? (
            <div
              style={{
                fontSize: "0.75rem",
                color: "var(--danger, #ef4444)",
                marginTop: "0.375rem",
              }}
            >
              {mine.inert_grants.length} grant(s) point at a target that no longer
              exists and confer nothing:{" "}
              {mine.inert_grants
                .map((g) => `${g.scope_type} ${g.scope_ref} (${g.reason})`)
                .join(", ")}
              . An administrator must reassign or revoke them; a vanished target
              never widens reach.
            </div>
          ) : null}
        </div>
      ) : null}

      {canManage ? (
        <div style={card}>
          <div
            style={{ fontSize: "0.9375rem", fontWeight: 600, marginBottom: "0.625rem" }}
          >
            Grant scope
          </div>
          <span style={labelStyle}>Principal (Keycloak subject)</span>
          <input
            style={inputStyle}
            placeholder="e.g. 58602fcc-608d-43fc-8340-822af61c81ed"
            value={principal}
            onChange={(e) => setPrincipal(e.target.value)}
          />
          <span style={labelStyle}>
            Role this grant narrows (recorded so the strict preflight can tell
            whether an administrator would remain)
          </span>
          <select
            style={inputStyle}
            value={role}
            onChange={(e) => setRole(e.target.value)}
          >
            {["tenant_owner", "site_admin", "operator", "auditor", "viewer"].map(
              (r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ),
            )}
          </select>
          <span style={labelStyle}>Scope</span>
          <select
            style={inputStyle}
            value={scopeType}
            onChange={(e) => {
              setScopeType(e.target.value);
              setScopeRef("");
            }}
          >
            {(data?.scope_types ?? []).map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
          {scopeType !== "tenant" ? (
            refOptions.length > 0 ? (
              <select
                style={inputStyle}
                value={scopeRef}
                onChange={(e) => setScopeRef(e.target.value)}
              >
                <option value="">Select…</option>
                {refOptions.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            ) : (
              <input
                style={inputStyle}
                placeholder="scope reference"
                value={scopeRef}
                onChange={(e) => setScopeRef(e.target.value)}
              />
            )
          ) : null}
          <input
            style={inputStyle}
            placeholder="Note (why this grant exists)"
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <button
            style={button("primary")}
            disabled={
              !principal.trim() || (scopeType !== "tenant" && !scopeRef.trim())
            }
            onClick={grant}
          >
            Grant
          </button>
        </div>
      ) : null}

      <div style={card}>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: "0.625rem",
          }}
        >
          <div style={{ fontSize: "0.9375rem", fontWeight: 600 }}>
            Grants ({data?.grants.length ?? 0})
          </div>
          <label style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
            <input
              type="checkbox"
              checked={showRevoked}
              onChange={(e) => setShowRevoked(e.target.checked)}
              style={{ marginRight: "0.375rem" }}
            />
            show revoked
          </label>
        </div>

        {!data || data.grants.length === 0 ? (
          <EmptyState
            title="No scope grants"
            description={
              strict
                ? "Under strict enforcement nobody reaches anything until granted."
                : "Nobody has been scoped yet, so every principal still reaches the whole tenant."
            }
          />
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table style={tableStyle}>
              <thead>
                <tr>
                  <th style={thStyle}>Principal</th>
                  <th style={thStyle}>Kind</th>
                  <th style={thStyle}>Role</th>
                  <th style={thStyle}>Scope</th>
                  <th style={thStyle}>Subset</th>
                  <th style={thStyle}>State</th>
                  <th style={thStyle}>Target</th>
                  {canManage ? <th style={thStyle} /> : null}
                </tr>
              </thead>
              <tbody>
                {data.grants.map((g) => (
                  <tr key={g.id}>
                    <td style={{ ...tdStyle, ...monoStyle }}>{g.principal_ref}</td>
                    <td style={tdStyle}>{g.principal_type}</td>
                    <td style={tdStyle}>{g.role || "—"}</td>
                    <td style={tdStyle}>
                      {g.scope_type}
                      {g.scope_ref ? (
                        <div style={monoStyle}>{g.scope_ref}</div>
                      ) : null}
                    </td>
                    <td style={tdStyle}>
                      {g.permission_subset
                        ? g.permission_subset.join(", ")
                        : "full role"}
                    </td>
                    <td style={tdStyle}>
                      {g.revoked_at ? (
                        <span style={chip("warn")}>revoked</span>
                      ) : g.expires_at ? (
                        <span style={chip("mute")}>expires</span>
                      ) : (
                        <span style={chip("ok")}>active</span>
                      )}
                    </td>
                    <td style={tdStyle}>
                      {g.target_status === "missing" ? (
                        <span style={chip("warn")} title="the target no longer exists; this grant reaches nothing">
                          missing
                        </span>
                      ) : g.target_status === "present" ? (
                        <span style={chip("ok")}>present</span>
                      ) : (
                        <span style={chip("mute")}>—</span>
                      )}
                    </td>
                    {canManage ? (
                      <td style={{ ...tdStyle, whiteSpace: "nowrap" }}>
                        {!g.revoked_at ? (
                          <>
                            <button
                              style={button("danger")}
                              onClick={() => revoke(g.id)}
                            >
                              Revoke
                            </button>{" "}
                            {g.principal_type === "user" ? (
                              <button
                                style={button("quiet")}
                                onClick={() =>
                                  setReassign({
                                    id: g.id,
                                    scopeType: g.scope_type === "tenant" ? "org_unit" : g.scope_type,
                                    scopeRef: "",
                                  })
                                }
                              >
                                Reassign
                              </button>
                            ) : null}
                          </>
                        ) : null}
                      </td>
                    ) : null}
                  </tr>
                ))}
              </tbody>
            </table>
            {reassign ? (
              <div style={{ marginTop: "0.75rem", maxWidth: "28rem" }}>
                <span style={labelStyle}>
                  Reassign grant {reassign.id} to a new target (the server checks
                  your reach and delegated permissions on both ends, and refuses
                  to move the last administrator)
                </span>
                <select
                  style={inputStyle}
                  value={reassign.scopeType}
                  onChange={(e) =>
                    setReassign({ ...reassign, scopeType: e.target.value, scopeRef: "" })
                  }
                >
                  {["org_unit", "site", "tenant", "device_class", "device"].map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
                {reassign.scopeType !== "tenant" ? (
                  <input
                    style={inputStyle}
                    placeholder="target id"
                    value={reassign.scopeRef}
                    onChange={(e) => setReassign({ ...reassign, scopeRef: e.target.value })}
                  />
                ) : null}
                <button style={button("primary")} onClick={submitReassign}>
                  Move grant
                </button>{" "}
                <button style={button("quiet")} onClick={() => setReassign(null)}>
                  Cancel
                </button>
              </div>
            ) : null}
          </div>
        )}
      </div>

      <Toast toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}
