/** A34.4: the Console's URL contract.
 *
 *  Every Console URL that a person can land on is declared here, and every
 *  URL that names an object is BUILT here -- so a link somewhere in the
 *  product and the page that opens it cannot disagree about its shape.
 *  `routes.test.ts` holds this table to `App.tsx` in both directions: a
 *  route declared in one and not the other fails the suite.
 *
 *  Rules the shapes follow:
 *
 *  - The tenant is a path segment, never a header or storage (E1.4).
 *  - An object is an OPTIONAL segment on its page's own route, so the list
 *    and the open object are one page instance, Back closes the object, and
 *    every pre-PX1A URL still resolves.
 *  - An object is named by its STABLE identity. A device is its Harken Node
 *    `agent_id`: the `cc_fleet_cache` row id is deleted and re-inserted on
 *    every poll and cannot be linked to.
 *  - Collection state (filters, tab, page) lives in the query string.
 *
 *  A URL is an address, not a permission. Opening one asks Central Command
 *  like any other read, and an object it does not return reads as "not
 *  found or not visible in your current scope" -- exactly what its 404
 *  means.
 */

import { matchPath } from "react-router-dom";
import { PLANES } from "./terminology";

export type Plane = "tenant" | "platform";

export interface PageRoute {
  /** Stable page id: the sidebar and access-rule key without the tenant. */
  id: string;
  plane: Plane;
  /** The react-router pattern, exactly as `App.tsx` declares it. */
  path: string;
  /** The page's own heading; also its document title. */
  title: string;
}

const T = "/t/:tenantId";

export const PAGE_ROUTES: readonly PageRoute[] = [
  // Tenant plane
  { id: "/dashboard", plane: "tenant", path: `${T}/dashboard`, title: "Overview" },
  { id: "/users", plane: "tenant", path: `${T}/users`, title: "Users & Roles" },
  { id: "/licenses", plane: "tenant", path: `${T}/licenses`, title: "Licenses" },
  { id: "/incidents", plane: "tenant", path: `${T}/incidents/:incidentId?`, title: "Incidents & Diagnosis" },
  { id: "/fleet", plane: "tenant", path: `${T}/fleet/:agentId?`, title: "Fleet Overview" },
  { id: "/risk", plane: "tenant", path: `${T}/risk`, title: "Risk & Exposure" },
  { id: "/learning", plane: "tenant", path: `${T}/learning`, title: "Learning" },
  { id: "/autonomy", plane: "tenant", path: `${T}/autonomy`, title: "Autonomy" },
  { id: "/capabilities", plane: "tenant", path: `${T}/capabilities`, title: "Capabilities" },
  { id: "/campaigns", plane: "tenant", path: `${T}/campaigns/:campaignId?`, title: "Campaigns" },
  { id: "/organization", plane: "tenant", path: `${T}/organization`, title: "Organization" },
  { id: "/access-scope", plane: "tenant", path: `${T}/access-scope`, title: "Access Scope" },
  {
    id: "/operational-agents",
    plane: "tenant",
    path: `${T}/operational-agents/:agentId?`,
    title: "Operational Agents",
  },
  { id: "/reliability", plane: "tenant", path: `${T}/reliability`, title: "Vendor Reliability" },
  { id: "/approvals", plane: "tenant", path: `${T}/approvals/:origin?/:itemId?`, title: "Approval Queue" },
  { id: "/agents", plane: "tenant", path: `${T}/agents`, title: "Harken Nodes" },
  { id: "/policies", plane: "tenant", path: `${T}/policies`, title: "Approval Policies" },
  { id: "/billing", plane: "tenant", path: `${T}/billing`, title: "Billing" },
  { id: "/invoices", plane: "tenant", path: `${T}/invoices/:id`, title: "Invoice" },
  { id: "/usage", plane: "tenant", path: `${T}/usage`, title: "Usage & Chargeback" },
  { id: "/support", plane: "tenant", path: `${T}/support`, title: "Support" },
  { id: "/audit", plane: "tenant", path: `${T}/audit`, title: "Audit Logs" },
  { id: "/downloads", plane: "tenant", path: `${T}/downloads`, title: "Downloads & Upgrades" },
  { id: "/marketplace", plane: "tenant", path: `${T}/marketplace`, title: "Skill Marketplace" },
  // Platform plane: administering the platform, inside no tenant.
  { id: "/tenants", plane: "platform", path: "/tenants", title: "Tenants" },
  { id: "/admin", plane: "platform", path: "/admin", title: "Admin Dashboard" },
  { id: "/admin/billing", plane: "platform", path: "/admin/billing", title: "Billing Administration" },
  { id: "/admin/features", plane: "platform", path: "/admin/features", title: "Feature Toggles" },
  { id: "/admin/releases", plane: "platform", path: "/admin/releases", title: "Releases" },
  { id: "/admin/health", plane: "platform", path: "/admin/health", title: "Platform Health" },
  { id: "/admin/support-access", plane: "platform", path: "/admin/support-access", title: "Support Access" },
  { id: "/marketplace", plane: "platform", path: "/marketplace", title: "Skill Marketplace" },
];

/** Routes `App.tsx` declares that are not pages: the root and catch-all
 *  redirects and the sign-in flow. Listed so the contract test can tell an
 *  undeclared page from a known non-page. */
export const NON_PAGE_PATHS: readonly string[] = ["/", "/login", "/callback", "/*"];

/** Object URLs whose shape is fixed now and whose page does not exist yet.
 *  No route serves them, and no builder hands one out: a link to a page
 *  that does not exist would be a promise the product does not keep. */
export const RESERVED_ROUTES = [
  {
    id: "resource",
    path: `${T}/resources/:resourceId`,
    needs:
      "a Resource Catalogue page, and the Console proxy forwarding `resources` " +
      "to Central Command -- a Console backend change with its own boundary",
  },
] as const;

/* ── Builders ─────────────────────────────────────────────────────────── */

/** The queue lanes an approval item can sit in, in the server's spelling
 *  (`ApprovalAction.origin`). A lane, never a provenance (A27.6). */
export const APPROVAL_ORIGINS = ["node", "agent", "agent_activation"] as const;
export type ApprovalOrigin = (typeof APPROVAL_ORIGINS)[number];

export type Query = Record<string, string | number | null | undefined>;

function seg(value: string): string {
  return encodeURIComponent(value);
}

/** `?a=1&b=2`, dropping empty values; "" when nothing is left. */
export function queryString(query?: Query): string {
  if (!query) return "";
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined || value === "") continue;
    params.set(key, String(value));
  }
  const s = params.toString();
  return s ? `?${s}` : "";
}

const tenant = (tenantId: string) => `/t/${seg(tenantId)}`;

export const links = {
  incidents: (tenantId: string, query?: Query) =>
    `${tenant(tenantId)}/incidents${queryString(query)}`,
  incident: (tenantId: string, incidentId: string) =>
    `${tenant(tenantId)}/incidents/${seg(incidentId)}`,

  fleet: (tenantId: string, query?: Query) => `${tenant(tenantId)}/fleet${queryString(query)}`,
  /** A device, by its Harken Node `agent_id` -- the identity that survives
   *  a poll. `atSiteId` is needed only while one node is reported at two
   *  sites at once; it is not the list's `site_id` filter, so following it
   *  does not narrow the list behind the drawer. */
  device: (tenantId: string, agentId: string, atSiteId?: string) =>
    `${tenant(tenantId)}/fleet/${seg(agentId)}${queryString({ at: atSiteId })}`,
  /** A site, as the fleet filtered to it: no site page exists yet. */
  site: (tenantId: string, siteId: string) => `${tenant(tenantId)}/fleet${queryString({ site_id: siteId })}`,

  operationalAgents: (tenantId: string) => `${tenant(tenantId)}/operational-agents`,
  operationalAgent: (tenantId: string, agentId: string) =>
    `${tenant(tenantId)}/operational-agents/${seg(agentId)}`,

  campaigns: (tenantId: string) => `${tenant(tenantId)}/campaigns`,
  campaign: (tenantId: string, campaignId: string) =>
    `${tenant(tenantId)}/campaigns/${seg(campaignId)}`,

  approvals: (tenantId: string, query?: Query) =>
    `${tenant(tenantId)}/approvals${queryString(query)}`,
  approvalItem: (tenantId: string, origin: ApprovalOrigin, itemId: string) =>
    `${tenant(tenantId)}/approvals/${origin}/${seg(itemId)}`,
  /** An Operational Agent proposal is an `agent` item in the approval queue. */
  proposal: (tenantId: string, proposalId: string) =>
    `${tenant(tenantId)}/approvals/agent/${seg(proposalId)}`,
} as const;

/* ── Reading a URL ────────────────────────────────────────────────────── */

/** The page a pathname belongs to, or null for a non-page. An object URL
 *  belongs to its page: opening an incident is still the Incidents page. */
export function pageForPath(pathname: string): PageRoute | null {
  for (const page of PAGE_ROUTES) {
    if (matchPath({ path: page.path, end: true }, pathname)) return page;
  }
  return null;
}

const PLANE_NAME: Record<Plane, string> = {
  tenant: PLANES.centralCommand,
  platform: "HarkenIQ Platform Console",
};

/** `Incidents & Diagnosis · HarkenIQ Central Command`. */
export function documentTitle(page: PageRoute | null): string {
  return page ? `${page.title} · ${PLANE_NAME[page.plane]}` : "HarkenIQ";
}
