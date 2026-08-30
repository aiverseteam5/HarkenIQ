import { useParams } from "react-router-dom";
import { type CSSProperties, useCallback, useEffect, useMemo, useState } from "react";
import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";
import { deleteJson, getJson, patchJson, postJson, putJson } from "../api";
import { useAuth } from "../useAuth";

/* E1.1 — Organization: the tenant's own containment tree.
 *
 * A customer describes its structure in its own words — regions,
 * clusters, circles, trusts, territories, availability zones — and
 * attaches each site to exactly one node of it.
 *
 * What this page is NOT, and the distinction is the product's:
 * containment is not authorization. Putting a site under Region West
 * says where it sits on the org chart. It grants nobody anything.
 * Reaching a site is a scope grant, a separate model that arrives with
 * E1.2, and an org-chart edit must never be a privilege change.
 *
 * This page is a consumer of /api/org-units. Every rule it appears to
 * enforce — depth, cycles, sibling names, delete-with-contents — is
 * enforced at Central Command; the UI only renders the refusal. Hiding
 * a button is never the authorization boundary. */

/* ── Types ────────────────────────────────────────── */

interface UnitNode {
  id: string;
  parent_id: string | null;
  unit_type: string;
  name: string;
  path: string;
  depth: number;
  sort_order: number;
  site_count: number;
  subtree_site_count?: number;
  children: UnitNode[];
}

interface TreeResponse {
  tenant_id: string;
  max_depth: number;
  unit_count: number;
  tree: UnitNode[];
}

interface UnitDetail {
  unit: UnitNode;
  ancestors: UnitNode[];
  children: UnitNode[];
  sites: { id: string; site_name: string; status: string }[];
  subtree_site_count: number;
}

interface SiteRow {
  id: string;
  site_name: string;
  status: string;
  org_unit_id: string | null;
}

/* ── Styles ───────────────────────────────────────── */

const layout: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "minmax(0, 1.4fr) minmax(0, 1fr)",
  gap: "1.25rem",
  alignItems: "start",
};

const card: CSSProperties = {
  background: "var(--surface)",
  border: "1px solid var(--border)",
  borderRadius: "8px",
  padding: "1rem 1.125rem",
};

const noteStyle: CSSProperties = {
  ...card,
  marginBottom: "1.25rem",
  borderLeft: "3px solid var(--accent, #3b82f6)",
  fontSize: "0.8125rem",
  color: "var(--text-muted)",
  lineHeight: 1.55,
};

const rowStyle = (selected: boolean, depth: number): CSSProperties => ({
  display: "flex",
  alignItems: "center",
  gap: "0.5rem",
  padding: "0.375rem 0.5rem",
  paddingLeft: `${0.5 + (depth - 1) * 1.125}rem`,
  borderRadius: "6px",
  cursor: "pointer",
  background: selected ? "var(--surface-hover, rgba(59,130,246,.10))" : "transparent",
  fontSize: "0.875rem",
});

const typeChip: CSSProperties = {
  fontSize: "0.6875rem",
  letterSpacing: "0.04em",
  textTransform: "uppercase",
  color: "var(--text-muted)",
  border: "1px solid var(--border)",
  borderRadius: "4px",
  padding: "0 0.3125rem",
  whiteSpace: "nowrap",
};

const countChip: CSSProperties = {
  ...typeChip,
  textTransform: "none",
  letterSpacing: 0,
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

const buttonStyle = (kind: "primary" | "quiet" | "danger"): CSSProperties => ({
  padding: "0.375rem 0.75rem",
  fontSize: "0.8125rem",
  borderRadius: "6px",
  cursor: "pointer",
  border: "1px solid var(--border)",
  background:
    kind === "primary" ? "var(--accent, #3b82f6)" : "transparent",
  color:
    kind === "primary"
      ? "#fff"
      : kind === "danger"
        ? "var(--danger, #ef4444)"
        : "var(--text)",
});

const pathStyle: CSSProperties = {
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
  fontSize: "0.6875rem",
  color: "var(--text-muted)",
  wordBreak: "break-all",
};

/* ── Helpers ──────────────────────────────────────── */

function flatten(nodes: UnitNode[]): UnitNode[] {
  return nodes.flatMap((n) => [n, ...flatten(n.children)]);
}

/* ── Page ─────────────────────────────────────────── */

export default function Organization() {
  const { tenantId = "" } = useParams<{ tenantId: string }>();
  const { user } = useAuth();
  const { toasts, toast, dismiss } = useToast();

  const [tree, setTree] = useState<TreeResponse | null>(null);
  const [detail, setDetail] = useState<UnitDetail | null>(null);
  const [sites, setSites] = useState<SiteRow[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [loading, setLoading] = useState(true);

  const [newName, setNewName] = useState("");
  const [newType, setNewType] = useState("region");
  const [renameTo, setRenameTo] = useState("");
  const [moveTo, setMoveTo] = useState("");
  const [attachSite, setAttachSite] = useState("");

  // The server is the authorization boundary; this only decides whether
  // to render controls that would 403 anyway.
  const perms = user?.permissions ?? [];
  const canManage = perms.includes("*") || perms.includes("site.manage");

  const load = useCallback(async () => {
    try {
      const [t, s] = await Promise.all([
        getJson<TreeResponse>(`/api/t/${tenantId}/org-units/`),
        getJson<{ sites: SiteRow[] } | SiteRow[]>(`/api/t/${tenantId}/sites/`),
      ]);
      setTree(t);
      setSites(Array.isArray(s) ? s : s.sites);
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setLoading(false);
    }
  }, [tenantId, toast]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    void (async () => {
      try {
        const d = await getJson<UnitDetail>(
          `/api/t/${tenantId}/org-units/${selected}`,
        );
        setDetail(d);
        setRenameTo(d.unit.name);
        setMoveTo(d.unit.parent_id ?? "");
      } catch (err) {
        toast((err as Error).message, "error");
      }
    })();
  }, [selected, tenantId, toast, tree]);

  const flat = useMemo(() => (tree ? flatten(tree.tree) : []), [tree]);

  const unattached = useMemo(
    () => sites.filter((s) => !s.org_unit_id),
    [sites],
  );

  async function run(work: () => Promise<unknown>, ok: string) {
    try {
      await work();
      toast(ok, "success");
      await load();
    } catch (err) {
      // Every refusal below is the server's, rendered verbatim: depth,
      // cycle, sibling collision, delete-with-contents.
      toast((err as Error).message, "error");
    }
  }

  const create = () =>
    run(async () => {
      await postJson(`/api/t/${tenantId}/org-units/`, {
        name: newName,
        unit_type: newType,
        parent_id: selected || null,
      });
      setNewName("");
    }, `Created ${newName}`);

  const rename = () =>
    run(
      () =>
        patchJson(`/api/t/${tenantId}/org-units/${selected}`, {
          name: renameTo,
        }),
      "Renamed",
    );

  const move = () =>
    run(
      () =>
        patchJson(`/api/t/${tenantId}/org-units/${selected}`, {
          parent_id: moveTo || null,
        }),
      "Moved",
    );

  const remove = () =>
    run(async () => {
      await deleteJson(`/api/t/${tenantId}/org-units/${selected}`);
      setSelected("");
    }, "Deleted");

  const attach = () =>
    run(async () => {
      await putJson(`/api/t/${tenantId}/sites/${attachSite}/org-unit`, {
        org_unit_id: selected,
      });
      setAttachSite("");
    }, "Site attached");

  if (loading) return <div style={{ padding: "1rem" }}>Loading…</div>;

  return (
    <div>
      <PageHeader
        title="Organization"
        breadcrumbs={[{ label: "Tenant" }, { label: "Organization" }]}
      />

      <div style={noteStyle}>
        <strong>Containment, not permission.</strong> This tree describes how
        the estate is organized and which unit each site belongs to. It grants
        nobody access to anything: who may reach a site is a separate scope
        grant. Depth is bounded at {tree?.max_depth ?? 8} levels, a unit cannot
        be moved beneath itself, and a unit holding children or sites is not
        deletable — every one of those is refused at Central Command, not here.
      </div>

      {!tree || tree.tree.length === 0 ? (
        <EmptyState
          title="No organizational units yet"
          description={
            canManage
              ? "Create a root unit to describe this tenant's structure."
              : "An administrator has not described this tenant's structure yet."
          }
        />
      ) : null}

      <div style={layout}>
        <div style={card}>
          <div
            style={{
              fontSize: "0.75rem",
              color: "var(--text-muted)",
              marginBottom: "0.625rem",
            }}
          >
            {tree?.unit_count ?? 0} unit{tree?.unit_count === 1 ? "" : "s"}
            {" · "}
            {sites.length} site{sites.length === 1 ? "" : "s"}
            {unattached.length > 0
              ? ` · ${unattached.length} unattached`
              : ""}
          </div>

          {flat.map((node) => (
            <div
              key={node.id}
              style={rowStyle(node.id === selected, node.depth)}
              onClick={() => setSelected(node.id === selected ? "" : node.id)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  setSelected(node.id === selected ? "" : node.id);
                }
              }}
            >
              <span style={{ flex: 1, minWidth: 0 }}>{node.name}</span>
              <span style={typeChip}>{node.unit_type}</span>
              {node.site_count > 0 ? (
                <span style={countChip}>
                  {node.site_count} site{node.site_count === 1 ? "" : "s"}
                </span>
              ) : null}
            </div>
          ))}

          {canManage ? (
            <div
              style={{
                marginTop: "1rem",
                paddingTop: "0.875rem",
                borderTop: "1px solid var(--border)",
              }}
            >
              <span style={labelStyle}>
                New unit{" "}
                {selected && detail
                  ? `under ${detail.unit.name}`
                  : "at the root"}
              </span>
              <input
                style={inputStyle}
                placeholder="Name, e.g. Region West"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
              />
              <input
                style={inputStyle}
                placeholder="Your word for this level, e.g. region"
                value={newType}
                onChange={(e) => setNewType(e.target.value)}
              />
              <button
                style={buttonStyle("primary")}
                disabled={!newName.trim()}
                onClick={create}
              >
                Create unit
              </button>
            </div>
          ) : null}
        </div>

        <div style={card}>
          {!detail ? (
            <div style={{ fontSize: "0.8125rem", color: "var(--text-muted)" }}>
              Select a unit to see what it contains.
            </div>
          ) : (
            <>
              <div
                style={{
                  fontSize: "0.75rem",
                  color: "var(--text-muted)",
                  marginBottom: "0.25rem",
                }}
              >
                {detail.ancestors.map((a) => a.name).join(" › ") || "root"}
              </div>
              <div style={{ fontSize: "1rem", fontWeight: 600 }}>
                {detail.unit.name}
              </div>
              <div style={{ ...pathStyle, margin: "0.375rem 0 0.875rem" }}>
                level {detail.unit.depth} · {detail.unit.path}
              </div>

              <div
                style={{
                  fontSize: "0.8125rem",
                  marginBottom: "0.875rem",
                }}
              >
                {detail.children.length} child unit
                {detail.children.length === 1 ? "" : "s"} ·{" "}
                {detail.sites.length} site
                {detail.sites.length === 1 ? "" : "s"} here ·{" "}
                {detail.subtree_site_count} in this subtree
              </div>

              {detail.sites.length > 0 ? (
                <ul
                  style={{
                    margin: "0 0 0.875rem",
                    paddingLeft: "1.125rem",
                    fontSize: "0.8125rem",
                  }}
                >
                  {detail.sites.map((s) => (
                    <li key={s.id}>
                      {s.site_name}{" "}
                      <span style={{ color: "var(--text-muted)" }}>
                        ({s.status})
                      </span>
                    </li>
                  ))}
                </ul>
              ) : null}

              {canManage ? (
                <div
                  style={{
                    paddingTop: "0.875rem",
                    borderTop: "1px solid var(--border)",
                  }}
                >
                  <span style={labelStyle}>Rename</span>
                  <input
                    style={inputStyle}
                    value={renameTo}
                    onChange={(e) => setRenameTo(e.target.value)}
                  />
                  <button
                    style={buttonStyle("quiet")}
                    disabled={!renameTo.trim() || renameTo === detail.unit.name}
                    onClick={rename}
                  >
                    Rename
                  </button>

                  <span style={{ ...labelStyle, marginTop: "0.875rem" }}>
                    Move under
                  </span>
                  <select
                    style={inputStyle}
                    value={moveTo}
                    onChange={(e) => setMoveTo(e.target.value)}
                  >
                    <option value="">(make this a root unit)</option>
                    {flat
                      .filter((n) => !n.path.startsWith(detail.unit.path))
                      .map((n) => (
                        <option key={n.id} value={n.id}>
                          {"— ".repeat(n.depth - 1)}
                          {n.name}
                        </option>
                      ))}
                  </select>
                  <button
                    style={buttonStyle("quiet")}
                    disabled={moveTo === (detail.unit.parent_id ?? "")}
                    onClick={move}
                  >
                    Move
                  </button>

                  <span style={{ ...labelStyle, marginTop: "0.875rem" }}>
                    Attach a site
                  </span>
                  <select
                    style={inputStyle}
                    value={attachSite}
                    onChange={(e) => setAttachSite(e.target.value)}
                  >
                    <option value="">Select a site…</option>
                    {sites
                      .filter((s) => s.org_unit_id !== detail.unit.id)
                      .map((s) => (
                        <option key={s.id} value={s.id}>
                          {s.site_name}
                          {s.org_unit_id ? " (move here)" : ""}
                        </option>
                      ))}
                  </select>
                  <button
                    style={buttonStyle("quiet")}
                    disabled={!attachSite}
                    onClick={attach}
                  >
                    Attach
                  </button>

                  <div
                    style={{
                      marginTop: "1rem",
                      paddingTop: "0.875rem",
                      borderTop: "1px solid var(--border)",
                    }}
                  >
                    <button style={buttonStyle("danger")} onClick={remove}>
                      Delete unit
                    </button>
                    <div
                      style={{
                        fontSize: "0.75rem",
                        color: "var(--text-muted)",
                        marginTop: "0.375rem",
                      }}
                    >
                      Refused while it holds child units or sites. Move them
                      first — a site with no organizational path is a site
                      nobody owns.
                    </div>
                  </div>
                </div>
              ) : null}
            </>
          )}
        </div>
      </div>

      <Toast toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}
