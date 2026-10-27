/** A34.4: the URL contract, held to the router in both directions.
 *
 *  A28.24 asks for "a route-contract test that every listed object is
 *  addressable". The structural half reads `App.tsx` the way the backend's
 *  route-contract tests read the running app: if a page is declared in one
 *  place and not the other, or an object has a URL shape no builder makes,
 *  the suite fails by name rather than a link quietly 404ing.
 */

import ts from "typescript";
// Read as text by Vite, so the contract needs no Node file-system types.
import appSource from "./App.tsx?raw";
import { describe, expect, it } from "vitest";
import { matchPath } from "react-router-dom";
import {
  APPROVAL_ORIGINS,
  NON_PAGE_PATHS,
  PAGE_ROUTES,
  RESERVED_ROUTES,
  documentTitle,
  links,
  pageForPath,
  queryString,
} from "./routes";
import { PLATFORM_NAV, TENANT_NAV } from "./layouts/SidebarLayout";

/** Every `path` a `<Route>` in App.tsx declares, read from the source. */
function declaredRoutePaths(): string[] {
  const source = ts.createSourceFile("App.tsx", appSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const paths: string[] = [];
  const visit = (node: ts.Node) => {
    if (
      (ts.isJsxSelfClosingElement(node) || ts.isJsxOpeningElement(node)) &&
      node.tagName.getText(source) === "Route"
    ) {
      for (const prop of node.attributes.properties) {
        if (ts.isJsxAttribute(prop) && prop.name.getText(source) === "path") {
          const init = prop.initializer;
          if (!init || !ts.isStringLiteral(init)) {
            throw new Error(`a <Route path> must be a string literal so this contract can read it: ${prop.getText(source)}`);
          }
          paths.push(init.text);
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return paths;
}

/** One sample object URL per optional object segment, and the params it
 *  must decode to. Adding an object segment to a page without a builder
 *  here fails `every optional object segment has a builder`. */
const OBJECT_LINKS: { page: string; param: string; href: string; expect: Record<string, string> }[] = [
  { page: "/incidents", param: "incidentId", href: links.incident("acme", "inc-1"), expect: { incidentId: "inc-1" } },
  { page: "/fleet", param: "agentId", href: links.device("acme", "a1b2c3d4e5f60718"), expect: { agentId: "a1b2c3d4e5f60718" } },
  { page: "/operational-agents", param: "agentId", href: links.operationalAgent("acme", "oa-9"), expect: { agentId: "oa-9" } },
  { page: "/campaigns", param: "campaignId", href: links.campaign("acme", "cmp-3"), expect: { campaignId: "cmp-3" } },
  { page: "/approvals", param: "origin", href: links.approvalItem("acme", "node", "r-1"), expect: { origin: "node", itemId: "r-1" } },
  { page: "/approvals", param: "itemId", href: links.proposal("acme", "p-7"), expect: { origin: "agent", itemId: "p-7" } },
];

function decoded(params: Record<string, string | undefined>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(params).map(([k, v]) => [k, decodeURIComponent(v ?? "")]),
  );
}

describe("App.tsx and routes.ts declare the same pages", () => {
  const declared = declaredRoutePaths();

  it("every page in the contract is a route the router serves", () => {
    const missing = PAGE_ROUTES.map((p) => p.path).filter((p) => !declared.includes(p));
    expect(missing, "declared in routes.ts but not in App.tsx").toEqual([]);
  });

  it("every route the router serves is a page in the contract, or a known non-page", () => {
    const known = new Set([...PAGE_ROUTES.map((p) => p.path), ...NON_PAGE_PATHS]);
    const unknown = declared.filter((p) => !known.has(p));
    expect(unknown, "declared in App.tsx but not in routes.ts").toEqual([]);
  });

  it("no path is declared twice", () => {
    const paths = PAGE_ROUTES.map((p) => p.path);
    expect(new Set(paths).size).toBe(paths.length);
    expect(new Set(declared).size).toBe(declared.length);
  });

  it("a reserved object URL is served by no route, so no link can promise it", () => {
    for (const reserved of RESERVED_ROUTES) {
      expect(declared).not.toContain(reserved.path);
      expect(pageForPath(reserved.path.replace(":tenantId", "acme").replace(":resourceId", "r-1"))).toBeNull();
    }
  });

  it("every sidebar entry opens a declared page on its own plane", () => {
    for (const [nav, plane, prefix] of [
      [TENANT_NAV, "tenant", "/t/acme"],
      [PLATFORM_NAV, "platform", ""],
    ] as const) {
      for (const item of nav.flatMap((s) => s.items)) {
        const page = pageForPath(`${prefix}${item.key}`);
        expect(page, `${item.label} (${item.key})`).not.toBeNull();
        expect(page!.plane).toBe(plane);
      }
    }
  });
});

describe("every object has a URL, and every object URL has a builder", () => {
  it("every optional object segment has a builder", () => {
    const optional = PAGE_ROUTES.flatMap((p) =>
      [...p.path.matchAll(/:(\w+)\?/g)].map((m) => `${p.id}:${m[1]}`),
    );
    const built = OBJECT_LINKS.map((o) => `${o.page}:${o.param}`);
    expect(new Set(built)).toEqual(new Set(optional));
  });

  it.each(OBJECT_LINKS)("$href opens $page with exactly its params", ({ page, href, expect: want }) => {
    const route = PAGE_ROUTES.find((p) => p.id === page && p.plane === "tenant")!;
    const url = new URL(href, "https://console.example");
    const match = matchPath({ path: route.path, end: true }, url.pathname);
    expect(match, href).not.toBeNull();
    expect(decoded(match!.params)).toEqual({ tenantId: "acme", ...want });
    expect(pageForPath(url.pathname)).toBe(route);
  });

  it("an id that contains a slash or a query character stays one segment", () => {
    const href = links.incident("acme", "a/b?c=d#e");
    const route = PAGE_ROUTES.find((p) => p.id === "/incidents")!;
    const match = matchPath({ path: route.path, end: true }, href);
    expect(decoded(match!.params).incidentId).toBe("a/b?c=d#e");
  });

  it("a device link carries a site only to disambiguate, and never as the list filter", () => {
    expect(links.device("acme", "n1")).toBe("/t/acme/fleet/n1");
    expect(links.device("acme", "n1", "site-b")).toBe("/t/acme/fleet/n1?at=site-b");
    expect(links.site("acme", "site-b")).toBe("/t/acme/fleet?site_id=site-b");
  });

  it("approval items are addressed by lane and id, in the server's lane spelling", () => {
    expect(APPROVAL_ORIGINS).toEqual(["node", "agent", "agent_activation"]);
    expect(links.proposal("acme", "p-1")).toBe(links.approvalItem("acme", "agent", "p-1"));
  });
});

describe("pre-PX1A URLs still resolve to the same page", () => {
  it.each([
    ["/t/acme/incidents", "/incidents"],
    ["/t/acme/fleet", "/fleet"],
    ["/t/acme/campaigns", "/campaigns"],
    ["/t/acme/operational-agents", "/operational-agents"],
    ["/t/acme/approvals", "/approvals"],
    ["/t/acme/dashboard", "/dashboard"],
    ["/admin/features", "/admin/features"],
  ])("%s", (pathname, id) => {
    expect(pageForPath(pathname)?.id).toBe(id);
  });
});

describe("query strings and titles", () => {
  it("drops empty values and keeps the rest", () => {
    expect(queryString({ status: "open", site_id: "", page: 2, q: null })).toBe("?status=open&page=2");
    expect(queryString({})).toBe("");
  });

  it("a page's document title is its heading and its plane", () => {
    expect(documentTitle(pageForPath("/t/acme/incidents/inc-1"))).toBe(
      "Incidents & Diagnosis · HarkenIQ Central Command",
    );
    expect(documentTitle(pageForPath("/admin/health"))).toBe("Platform Health · HarkenIQ Platform Console");
    expect(documentTitle(null)).toBe("HarkenIQ");
  });
});
