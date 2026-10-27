import { useParams } from "react-router-dom";
import { type CSSProperties, useCallback, useEffect, useId, useMemo, useState } from "react";
import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";
import Banner from "../components/Banner";
import DataState from "../components/DataState";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";
import { deleteJson, getJson, patchJson, postJson, putJson } from "../api";
import { useAuth } from "../useAuth";
import { authoritativeSites, type MaybeContextualSite } from "../viewModel/siteContext";

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

interface SiteRow extends MaybeContextualSite {
  id: string;
  site_name: string;
  status: string;
  org_unit_id: string | null;
}

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
  const fieldId = useId();

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
      // A30.25: an authority surface offers the sites the caller HOLDS.
      setSites(authoritativeSites(Array.isArray(s) ? s : s.sites));
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

  if (loading) return <DataState status="loading" subject="the organization" />;

  return (
    <div>
      <PageHeader
        title="Organization"
        breadcrumbs={[{ label: "Tenant" }, { label: "Organization" }]}
      />

      <Banner tone="informational" title="Containment, not permission." className="page-notice">
        This tree describes how the estate is organized and which unit each
        site belongs to. It grants nobody access to anything: who may reach a
        site is a separate scope grant. Depth is bounded at{" "}
        {tree?.max_depth ?? 8} levels, a unit cannot be moved beneath itself,
        and a unit holding children or sites is not deletable — every one of
        those is refused at Central Command, not here.
      </Banner>

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

      <div className="split">
        <div className="card">
          <p className="t-meta">
            {tree?.unit_count ?? 0} unit{tree?.unit_count === 1 ? "" : "s"}
            {" · "}
            {sites.length} site{sites.length === 1 ? "" : "s"}
            {unattached.length > 0 ? ` · ${unattached.length} unattached` : ""}
          </p>

          <div className="tree" role="group" aria-label="Organizational units">
            {flat.map((node) => (
              <button
                key={node.id}
                type="button"
                className="tree-row"
                aria-pressed={node.id === selected}
                style={{ "--tree-depth": node.depth - 1 } as CSSProperties}
                onClick={() => setSelected(node.id === selected ? "" : node.id)}
              >
                <span className="tree-row__name">{node.name}</span>
                <span className="chip">{node.unit_type}</span>
                {node.site_count > 0 ? (
                  <span className="chip">
                    {node.site_count} site{node.site_count === 1 ? "" : "s"}
                  </span>
                ) : null}
              </button>
            ))}
          </div>

          {canManage ? (
            <div className="form-section">
              <div className="t-label">
                New unit {selected && detail ? `under ${detail.unit.name}` : "at the root"}
              </div>
              <input
                className="input"
                aria-label="Unit name"
                placeholder="Name, e.g. Region West"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
              />
              <input
                className="input"
                aria-label="Unit type"
                placeholder="Your word for this level, e.g. region"
                value={newType}
                onChange={(e) => setNewType(e.target.value)}
              />
              <button
                type="button"
                className="btn btn-primary"
                disabled={!newName.trim()}
                onClick={create}
              >
                Create unit
              </button>
            </div>
          ) : null}
        </div>

        <div className="card">
          {!detail ? (
            <p className="t-support">Select a unit to see what it contains.</p>
          ) : (
            <>
              <div className="t-meta">
                {detail.ancestors.map((a) => a.name).join(" › ") || "root"}
              </div>
              <h2 className="t-object-title">{detail.unit.name}</h2>
              <div className="t-meta">
                level {detail.unit.depth} · <code>{detail.unit.path}</code>
              </div>

              <p className="t-support org-detail__counts">
                {detail.children.length} child unit
                {detail.children.length === 1 ? "" : "s"} ·{" "}
                {detail.sites.length} site
                {detail.sites.length === 1 ? "" : "s"} here ·{" "}
                {detail.subtree_site_count} in this subtree
              </p>

              {detail.sites.length > 0 ? (
                <ul className="bullet-list t-support">
                  {detail.sites.map((s) => (
                    <li key={s.id}>
                      {s.site_name} <span className="text-muted">({s.status})</span>
                    </li>
                  ))}
                </ul>
              ) : null}

              {canManage ? (
                <>
                  <div className="form-section">
                    <label className="t-label" htmlFor={`${fieldId}-rename`}>
                      Rename
                    </label>
                    <input
                      id={`${fieldId}-rename`}
                      className="input"
                      value={renameTo}
                      onChange={(e) => setRenameTo(e.target.value)}
                    />
                    <button
                      type="button"
                      className="btn"
                      disabled={!renameTo.trim() || renameTo === detail.unit.name}
                      onClick={rename}
                    >
                      Rename
                    </button>
                  </div>

                  <div className="form-section">
                    <label className="t-label" htmlFor={`${fieldId}-move`}>
                      Move under
                    </label>
                    <select
                      id={`${fieldId}-move`}
                      className="select"
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
                      type="button"
                      className="btn"
                      disabled={moveTo === (detail.unit.parent_id ?? "")}
                      onClick={move}
                    >
                      Move
                    </button>
                  </div>

                  <div className="form-section">
                    <label className="t-label" htmlFor={`${fieldId}-attach`}>
                      Attach a site
                    </label>
                    <select
                      id={`${fieldId}-attach`}
                      className="select"
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
                    <button type="button" className="btn" disabled={!attachSite} onClick={attach}>
                      Attach
                    </button>
                  </div>

                  <div className="form-section">
                    <button type="button" className="btn btn-danger" onClick={remove}>
                      Delete unit
                    </button>
                    <p className="t-meta">
                      Refused while it holds child units or sites. Move them
                      first — a site with no organizational path is a site
                      nobody owns.
                    </p>
                  </div>
                </>
              ) : null}
            </>
          )}
        </div>
      </div>

      <Toast toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}
