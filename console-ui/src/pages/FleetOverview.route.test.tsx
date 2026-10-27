// @vitest-environment jsdom
/** A34.4: a device is addressed by its Harken Node id.
 *
 *  The drawer used to fetch `/fleet/{cc_fleet_cache.id}` -- a row the poller
 *  deletes and re-inserts every cycle, so the only id the page had was not
 *  linkable. These tests hold the link to the identity that survives a
 *  poll, resolved through the same scoped read the list makes.
 */

import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import FleetOverview from "./FleetOverview";
import { renderAtRoute } from "../test/render";
import { stubFetch, type StubRoutes } from "../test/fetch";

const PATH = "/t/:tenantId/fleet/:agentId?";
const NODE = "a1b2c3d4e5f60718";

function row(id: string, site: string, name = "rack7-u12") {
  return {
    id,
    tenant_id: "acme",
    site_id: site,
    site_name: site === "site-a" ? "Pune DC1" : "Austin",
    agent_id: NODE,
    agent_name: name,
    name,
    vendor: "Dell",
    model: "R760",
    device_class: "server",
    service_tag: "7X9K2L1",
    health: "ok",
    observation: "normal",
    tier: "observe",
    last_seen_at: null,
  };
}

function detail(id: string, site: string) {
  return { ...row(id, site), subsystems_json: {}, warranty: null, firmware: [] };
}

function routes(over: StubRoutes = {}): StubRoutes {
  return {
    "/api/t/acme/sites": { body: { sites: [] } },
    "/api/t/acme/fleet/summary": { body: { total_nodes: 1, by_health: { ok: 1 }, incidents_open: 0, sites_count: 1 } },
    "/api/t/acme/predictive/risk": { body: { risks: [] } },
    "/api/t/acme/firmware/exposure": { body: { exposures: [] } },
    "/api/t/acme/fleet": (url) => {
      const rowId = url.pathname.split("/")[5];
      if (rowId) return { body: detail(rowId, "site-a") };
      return { body: { items: [row("row-1", "site-a")], total: 1, page: 1, page_size: 20 } };
    },
    ...over,
  };
}

const location = () => screen.getByTestId("location").textContent;

describe("a device URL names the Harken Node, not the poll's row", () => {
  it("resolves the node id to the current row through the scoped fleet read", async () => {
    const { calls } = stubFetch(routes());
    renderAtRoute(<FleetOverview />, { route: `/t/acme/fleet/${NODE}`, path: PATH });
    await screen.findByRole("dialog", { name: "rack7-u12" });
    expect(calls).toContain(`/api/t/acme/fleet?search=${NODE}&page_size=200`);
    expect(calls).toContain("/api/t/acme/fleet/row-1");
  });

  it("a row the poll replaced between two reads is read again, not reported gone", async () => {
    let lists = 0;
    stubFetch(
      routes({
        "/api/t/acme/fleet": (url) => {
          const rowId = url.pathname.split("/")[5];
          if (rowId === "row-old") return { status: 404, body: { detail: "device not found" } };
          if (rowId) return { body: detail(rowId, "site-a") };
          lists += 1;
          const id = lists <= 2 ? "row-old" : "row-new";
          return { body: { items: [row(id, "site-a")], total: 1, page: 1, page_size: 20 } };
        },
      }),
    );
    renderAtRoute(<FleetOverview />, { route: `/t/acme/fleet/${NODE}`, path: PATH });
    expect(await screen.findByRole("dialog", { name: "rack7-u12" })).toBeTruthy();
  });

  it("a node reported at two sites is stated as two, never one chosen silently", async () => {
    stubFetch(
      routes({
        "/api/t/acme/fleet": (url) => {
          const rowId = url.pathname.split("/")[5];
          if (rowId) return { body: detail(rowId, rowId === "row-b" ? "site-b" : "site-a") };
          return { body: { items: [row("row-a", "site-a"), row("row-b", "site-b")], total: 2, page: 1, page_size: 20 } };
        },
      }),
    );
    const { user } = renderAtRoute(<FleetOverview />, { route: `/t/acme/fleet/${NODE}`, path: PATH });
    const drawer = await screen.findByRole("dialog");
    await within(drawer).findByText(/reported at more than one site/);
    await user.click(within(drawer).getByRole("link", { name: "Austin" }));
    expect(location()).toBe(`/t/acme/fleet/${NODE}?at=site-b`);
  });

  it("a node the reader cannot see reads as not found or not visible", async () => {
    stubFetch(
      routes({
        "/api/t/acme/fleet": { body: { items: [], total: 0, page: 1, page_size: 20 } },
      }),
    );
    renderAtRoute(<FleetOverview />, { route: `/t/acme/fleet/${NODE}`, path: PATH });
    const drawer = await screen.findByRole("dialog");
    await waitFor(() =>
      expect(within(drawer).getByRole("status").textContent).toContain("not visible in your current scope"),
    );
  });

  it("opening a row from the list keeps the list's filters in the URL", async () => {
    stubFetch(routes());
    const { user } = renderAtRoute(<FleetOverview />, { route: "/t/acme/fleet?vendor=Dell", path: PATH });
    await user.click(await screen.findByText(NODE));
    expect(location()).toBe(`/t/acme/fleet/${NODE}?vendor=Dell`);
  });
});

describe("filters and page are the query string", () => {
  it("a filtered, paged URL asks for exactly that page", async () => {
    const { calls } = stubFetch(routes());
    renderAtRoute(<FleetOverview />, { route: "/t/acme/fleet?vendor=Dell&page=2", path: PATH });
    await screen.findByText(NODE);
    expect(calls).toContain("/api/t/acme/fleet?vendor=Dell&page=2&page_size=20");
  });

  it("changing a filter returns to page one and drops it from the URL", async () => {
    stubFetch(routes());
    const { user } = renderAtRoute(<FleetOverview />, { route: "/t/acme/fleet?page=3", path: PATH });
    await screen.findByText(NODE);
    await user.selectOptions(screen.getAllByRole("combobox")[1], "HPE");
    expect(location()).toBe("/t/acme/fleet?vendor=HPE");
  });

  it("a site link is the fleet filtered to that site", async () => {
    const { calls } = stubFetch(routes());
    renderAtRoute(<FleetOverview />, { route: "/t/acme/fleet?site_id=site-a", path: PATH });
    await screen.findByText(NODE);
    expect(calls).toContain("/api/t/acme/fleet?site_id=site-a&page=1&page_size=20");
  });
});
