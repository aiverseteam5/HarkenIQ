import { useState } from "react";
import Sidebar from "../../components/Sidebar";
import PageHeader from "../../components/PageHeader";
import MetricCard from "../../components/MetricCard";
import DataTable, { type Column } from "../../components/DataTable";
import FilterBar from "../../components/FilterBar";
import StatusBadge from "../../components/StatusBadge";
import Banner from "../../components/Banner";
import Panel from "../../components/Panel";
import SectionHeading from "../../components/SectionHeading";
import { MetadataList, MetadataRow } from "../../components/MetadataList";
import EmptyState from "../../components/EmptyState";
import DataState from "../../components/DataState";
import Spinner from "../../components/Spinner";
import ConfirmDialog from "../../components/ConfirmDialog";
import DetailPanel from "../../components/DetailPanel";
import IconButton from "../../components/IconButton";
import { BrandLockup } from "../../components/BrandMark";
import { TENANT_NAV } from "../../layouts/SidebarLayout";
import { SEVERITY_TONES, STATE_TONES, statusTone } from "../../viewModel/status";
import { space, surface, text } from "../tokens";

/** A35.11: the dev-only primitive gallery (A28.24 anticipates one).
 *
 *  It renders the REAL shared components on fixture data, so the visual
 *  review package shows what pages will get. It is served by `vite` at
 *  /gallery.html, sits outside the route table, and is not part of the
 *  production build (only index.html is). `?section=<id>` renders one
 *  section alone, which is how the dialog and the drawer are captured. */

interface Row {
  agent_id: string;
  name: string;
  vendor: string;
  site: string;
  health: string;
  observation: string;
  last_seen: string;
}

const ROWS: Row[] = [
  { agent_id: "a3f9c2e10b7d4415", name: "lon-r12-u07", vendor: "Dell", site: "London-1", health: "critical", observation: "faulted", last_seen: "Oct 27, 14:02" },
  { agent_id: "7c01d9ee52a04b0f", name: "lon-r12-u09", vendor: "Dell", site: "London-1", health: "warning", observation: "degraded", last_seen: "Oct 27, 14:01" },
  { agent_id: "0O1lI0O1lI0O1lI0", name: "fra-r03-u21", vendor: "HPE", site: "Frankfurt-2", health: "ok", observation: "normal", last_seen: "Oct 27, 14:02" },
  { agent_id: "e44b8a6c19f3d270", name: "fra-sw-tor-02", vendor: "Arista", site: "Frankfurt-2", health: "ok", observation: "normal", last_seen: "Oct 27, 13:58" },
  { agent_id: "9d2c7b31f0e8a654", name: "nyc-r07-u33", vendor: "HPE", site: "New York-1", health: "unknown", observation: "unreported", last_seen: "--" },
];

const COLUMNS: Column<Row>[] = [
  { key: "agent_id", header: "Harken Node ID", sortKey: "agent_id", render: (r) => <code className="t-code">{r.agent_id}</code> },
  { key: "name", header: "Name", sortKey: "name" },
  { key: "vendor", header: "Vendor" },
  { key: "site", header: "Site", sortKey: "site" },
  {
    key: "health",
    header: "Health",
    sortKey: "health",
    render: (r) => <StatusBadge status={r.health} tone={statusTone("device.health", r.health)} size="sm" />,
  },
  {
    key: "observation",
    header: "Observation",
    render: (r) => <StatusBadge status={r.observation} tone={statusTone("device.observation", r.observation)} size="sm" />,
  },
  { key: "last_seen", header: "Last Seen" },
];

const pad = { padding: space["8"] };

function Section({ id, title, children }: { id: string; title: string; children: React.ReactNode }) {
  return (
    <section id={id} className="stack" style={{ marginBottom: space["12"] }} aria-labelledby={`${id}-title`}>
      <SectionHeading id={`${id}-title`}>{title}</SectionHeading>
      {children}
    </section>
  );
}

function Shell() {
  return (
    <div className="app-layout">
      <Sidebar
        sections={TENANT_NAV}
        activeItem="/fleet"
        hrefFor={(key) => `/t/acme${key}`}
        user={{ name: "Priya Raman", email: "priya@acme.example", role: "tenant_owner" }}
        onLogout={() => {}}
      />
      <div className="app-content">
        <section aria-label="Operational context" className="context-bar">
          <span className="context-bar__crumb">Tenant</span>
          <span className="context-bar__tenant">Acme Corp</span>
          <dl className="context-bar__facts">
            <div className="context-bar__fact">
              <dt>Scope</dt>
              <dd>Tenant-wide</dd>
            </div>
            <div className="context-bar__fact">
              <dt>Enforcement</dt>
              <dd>strict</dd>
            </div>
            <div className="context-bar__fact" data-emphasis="warning">
              <dt>Safety</dt>
              <dd>1 site not reporting</dd>
            </div>
            <div className="context-bar__fact">
              <dt>Data</dt>
              <dd>Updated 14:02</dd>
            </div>
          </dl>
        </section>
        <main>
          <PageHeader
            title="Fleet Overview"
            breadcrumbs={[{ label: "HarkenIQ" }, { label: "Fleet", href: "/t/acme/fleet" }, { label: "Overview" }]}
            actions={[{ label: "Export", onClick: () => {} }, { label: "Register site", onClick: () => {}, variant: "primary" }]}
          />
          <div className="metric-grid">
            <MetricCard title="Total Nodes" value={1284} />
            <MetricCard title="Healthy" value={96} unit="%" trend="up" trendValue="1.2" sparklineData={[91, 92, 94, 93, 95, 96]} />
            <MetricCard title="Open Incidents" value={7} trend="down" trendValue="3" />
            <MetricCard title="Sites" value={12} />
          </div>
        </main>
      </div>
    </div>
  );
}

function Table() {
  const [sort, setSort] = useState("health");
  return (
    <>
      <FilterBar
        filters={[
          { key: "q", label: "Devices", type: "text" },
          { key: "health", label: "Health", type: "select", options: [{ value: "critical", label: "critical" }] },
        ]}
        values={{ q: "lon-r12", health: "" }}
        onChange={() => {}}
        onClear={() => {}}
      />
      <DataTable<Row>
        columns={COLUMNS}
        data={ROWS}
        sortColumn={sort}
        sortDirection="asc"
        onSort={setSort}
        page={1}
        pageSize={20}
        total={1284}
        onPageChange={() => {}}
        onRowClick={() => {}}
        isRowSelected={(r) => r.agent_id === "7c01d9ee52a04b0f"}
        rowActions={[
          { label: "Locate", onClick: () => {} },
          { label: "Retire", variant: "danger", onClick: () => {} },
        ]}
      />
    </>
  );
}

function Form() {
  return (
    <div className="split">
      <Panel title="Controls">
        <div className="stack">
          <div className="field">
            <label className="t-label" htmlFor="g-name">Unit name</label>
            <input id="g-name" className="input" placeholder="Name, e.g. Region West" />
          </div>
          <div className="field">
            <label className="t-label" htmlFor="g-filled">Service tag</label>
            <input id="g-filled" className="input" defaultValue="7XK2QH3" />
          </div>
          <div className="field">
            <label className="t-label" htmlFor="g-invalid">Maintenance window</label>
            <input id="g-invalid" className="input" aria-invalid="true" aria-describedby="g-invalid-error" defaultValue="25:00" />
            <span id="g-invalid-error" className="field__error">Use a 24-hour time, 00:00 to 23:59.</span>
          </div>
          <div className="field">
            <label className="t-label" htmlFor="g-disabled">Tenant ID</label>
            <input id="g-disabled" className="input" disabled defaultValue="acme" />
            <span className="field__hint">Set when the tenant was created.</span>
          </div>
          <div className="field">
            <label className="t-label" htmlFor="g-select">Move under</label>
            <select id="g-select" className="select" defaultValue="west">
              <option value="west">Region West</option>
              <option value="east">Region East</option>
            </select>
          </div>
          <div className="field">
            <label className="t-label" htmlFor="g-notes">Notes</label>
            <textarea id="g-notes" className="textarea" defaultValue="Replace the PSU in bay 2 before the window." />
          </div>
          <label className="cluster t-body">
            <input type="checkbox" defaultChecked /> Notify the site contact
          </label>
        </div>
      </Panel>
      <Panel title="Buttons">
        <div className="stack">
          <div className="cluster">
            <button type="button" className="btn btn-primary">Approve</button>
            <button type="button" className="btn">Request evidence</button>
            <button type="button" className="btn btn-danger">Deny</button>
            <button type="button" className="btn btn-ghost">Clear all</button>
          </div>
          <div className="cluster">
            <button type="button" className="btn btn-sm btn-primary">Compact</button>
            <button type="button" className="btn btn-sm">Locate</button>
            <button type="button" className="btn btn-sm btn-danger">Retire</button>
          </div>
          <div className="cluster">
            <button type="button" className="btn" disabled>Disabled</button>
            <button type="button" className="btn btn-primary" aria-busy="true" disabled>
              <Spinner size="sm" label={null} /> Approving
            </button>
            <button type="button" className="btn" aria-pressed="true">Selected</button>
          </div>
          <div className="cluster">
            <IconButton icon="close" label="Close panel" />
            <IconButton icon="search" label="Search" />
          </div>
        </div>
      </Panel>
    </div>
  );
}

function Status() {
  return (
    <div className="stack">
      <Panel title="Severity: an observed condition">
        <div className="cluster">
          {SEVERITY_TONES.map((tone) => (
            <StatusBadge key={tone} status={tone} tone={tone} />
          ))}
        </div>
      </Panel>
      <Panel title="State: a governance or lifecycle position">
        <div className="cluster">
          {[...STATE_TONES, "unknown" as const].map((tone) => (
            <StatusBadge key={tone} status={tone} tone={tone} />
          ))}
        </div>
      </Panel>
      <Panel title="On the page: the same word, one tone">
        <div className="cluster">
          <StatusBadge status="approved" tone={statusTone("approval.decision", "approved")} size="sm" />
          <StatusBadge status="completed" tone={statusTone("campaign.wave", "completed")} size="sm" />
          <StatusBadge status="needs a human" tone={statusTone("proposal.attendance", "requires_human")} size="sm" />
          <StatusBadge status="unattended" tone={statusTone("proposal.attendance", "unattended")} size="sm" />
          <StatusBadge status="no stop in effect" tone={statusTone("safety.stop_switch", "inactive")} size="sm" />
          <StatusBadge status="low" tone={statusTone("action.risk", "low")} size="sm" />
          <StatusBadge status="denied" tone={statusTone("approval.decision", "denied")} size="sm" />
          <StatusBadge status="insufficient data" tone={statusTone("risk.band", "insufficient_data")} size="sm" />
        </div>
      </Panel>
      <Banner tone="critical" title="Stop switch active.">Every action class is denied until it is released.</Banner>
      <Banner tone="warning">1 site has not reported its safety state in 15 minutes.</Banner>
      <Banner tone="informational" title="Containment, not permission.">This tree grants nobody access to anything.</Banner>
      <Banner tone="stale">Showing this incident as of 14:02. The latest refresh failed: 503.</Banner>
    </div>
  );
}

function States() {
  return (
    <div className="split">
      <div className="stack">
        <div className="card">
          <EmptyState title="No incidents" description="Incidents appear here when a Harken Node reports a fault." />
        </div>
        <div className="card">
          <DataState status="loading" subject="this incident" />
        </div>
      </div>
      <div className="stack">
        <div className="card">
          <DataState status="stale" subject="this incident" lastSuccessAt={Date.UTC(2026, 9, 27, 14, 2)} failure={{ kind: "unavailable", status: 503, message: "no central_command registered" }}>
            <p className="t-support">The last loaded incident stays on screen.</p>
          </DataState>
        </div>
        <div className="card">
          <DataState status="not_found" subject="this incident" />
        </div>
        <div className="card">
          <DataState status="denied" subject="this incident" failure={{ kind: "denied", status: 403, message: "requires incident.view" }} />
        </div>
        <div className="card">
          <DataState status="unavailable" subject="this incident" failure={{ kind: "unavailable", status: 503, message: "no central_command registered" }} />
        </div>
      </div>
    </div>
  );
}

function ObjectView() {
  return (
    <>
      <PageHeader
        title="lon-r12-u07"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Fleet", href: "/t/acme/fleet" }, { label: "lon-r12-u07" }]}
        actions={[{ label: "Locate", onClick: () => {} }]}
      />
      <div className="split">
        <Panel title="Device info">
          <MetadataList>
            <MetadataRow label="Harken Node ID"><code>a3f9c2e10b7d4415</code></MetadataRow>
            <MetadataRow label="Service Tag"><code>7XK2QH3</code></MetadataRow>
            <MetadataRow label="Vendor / Model">Dell PowerEdge R750</MetadataRow>
            <MetadataRow label="Health">
              <StatusBadge status="critical" tone={statusTone("device.health", "critical")} size="sm" />
            </MetadataRow>
            <MetadataRow label="Warranty">
              <StatusBadge status="expiring" tone={statusTone("device.warranty", "expiring")} size="sm" />
            </MetadataRow>
          </MetadataList>
        </Panel>
        <Panel title="Failure risk">
          <MetadataList>
            <MetadataRow label="Band">
              <StatusBadge status="high" tone={statusTone("risk.band", "high")} size="sm" />
            </MetadataRow>
            <MetadataRow label="Score">71%</MetadataRow>
            <MetadataRow label="Plan hash"><code>9f2c0e1b7a6d…</code></MetadataRow>
          </MetadataList>
        </Panel>
      </div>
    </>
  );
}

function Typography() {
  return (
    <div className="split">
      <Panel title="Type roles">
        <div className="stack">
          <div className="t-page-title">Page title · Incidents &amp; Diagnosis</div>
          <div className="t-section">Section heading · Failure risk</div>
          <div className="t-object-title">Object title · lon-r12-u07</div>
          <div className="t-body">Body · The Harken Node reported a PSU fault at 14:02.</div>
          <div className="t-support">Supporting · Central Command gives the same answer for both.</div>
          <div className="t-label">Label · Maintenance window</div>
          <div className="t-column-header">Column header · Last seen</div>
          <div className="t-meta">Metadata · Updated 14:02 · 3 sites</div>
          <div className="t-kpi">1,284</div>
        </div>
      </Panel>
      <Panel title="Identifiers and the mark">
        <div className="stack">
          <code className="t-code">0O 1lI 8B 5S · a3f9c2e10b7d4415</code>
          <code className="t-code">sha256:9f2c0e1b7a6d44d1</code>
          <code className="t-code">10.20.0.17 · eth1/49 · 7XK2QH3</code>
          <BrandLockup />
          <div className="inverse-surface" style={{ background: surface.inverse, color: text.inverse, padding: space["4"] }}>
            <BrandLockup inverse />
          </div>
        </div>
      </Panel>
    </div>
  );
}

const SECTIONS: Record<string, [string, () => JSX.Element]> = {
  shell: ["Shell and header", Shell],
  table: ["Table", Table],
  form: ["Form controls and buttons", Form],
  status: ["Status and severity", Status],
  states: ["Empty, loading and error", States],
  object: ["Breadcrumbs and object header", ObjectView],
  type: ["Typography and brand", Typography],
};

export default function Gallery() {
  const only = new URLSearchParams(window.location.search).get("section");

  if (only === "dialog") {
    return (
      <div style={pad}>
        <ObjectView />
        <ConfirmDialog
          title="Deny this proposal?"
          message="The proposal is denied for good (D16): the Operational Agent can raise it again only as a new proposal."
          confirmLabel="Deny"
          variant="danger"
          onConfirm={() => {}}
          onCancel={() => {}}
        />
      </div>
    );
  }
  if (only === "drawer") {
    return (
      <div style={pad}>
        <Table />
        <DetailPanel open onClose={() => {}} title="lon-r12-u09" subtitle="7c01d9ee52a04b0f" width={520}>
          <SectionHeading level={3}>Device info</SectionHeading>
          <MetadataList>
            <MetadataRow label="Vendor">Dell</MetadataRow>
            <MetadataRow label="Service Tag"><code>5MPQ8R2</code></MetadataRow>
            <MetadataRow label="Health">
              <StatusBadge status="warning" tone={statusTone("device.health", "warning")} size="sm" />
            </MetadataRow>
          </MetadataList>
          <SectionHeading level={3}>Subsystem states</SectionHeading>
          <MetadataList>
            <MetadataRow label="psu">
              <StatusBadge status="warning" tone={statusTone("device.health", "warning")} size="sm" />
            </MetadataRow>
            <MetadataRow label="thermal">
              <StatusBadge status="ok" tone={statusTone("device.health", "ok")} size="sm" />
            </MetadataRow>
          </MetadataList>
        </DetailPanel>
      </div>
    );
  }
  if (only === "shell") return <Shell />;

  const entries = Object.entries(SECTIONS).filter(([id]) => id !== "shell" && (!only || id === only));
  return (
    <div style={pad}>
      {entries.map(([id, [title, Body]]) => (
        <Section key={id} id={id} title={title}>
          <Body />
        </Section>
      ))}
    </div>
  );
}
