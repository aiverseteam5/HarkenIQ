// @vitest-environment jsdom
/** A34.4: an incident is an address.
 *
 *  The ROUTE test pattern (A34.3): reach the page through a URL in a memory
 *  router, and assert what it asked Central Command for, what it shows, and
 *  where the URL goes when a person acts. Before PX1A the open incident and
 *  the status filter lived only in component state, so none of this was
 *  true: a reload dropped both, and nobody could send anyone a link.
 */

import { beforeEach, describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import Incidents from "./Incidents";
import { renderAtRoute } from "../test/render";
import { stubFetch } from "../test/fetch";
import { axeViolations } from "../test/axe";

const PATH = "/t/:tenantId/incidents/:incidentId?";

function incident(id: string, title: string) {
  return {
    incident_id: id,
    kind: "device",
    status: "open",
    title,
    device_agent_id: "a1b2c3d4e5f60718",
    subsystem: "psu",
    site_id: "site-a",
    site_name: "Pune DC1",
    parent_incident_id: null,
    is_parent: false,
    confidence: 0.9,
    inferred: false,
    correlation: {},
    diagnosis: null,
    opened_at: "2026-10-26T10:00:00Z",
    resolved_at: null,
    children: [],
    child_count: 0,
  };
}

function detail(id: string, title: string) {
  return {
    ...incident(id, title),
    prior_learning: [],
    current_state: { pending_approvals: [], open_action_count: 0 },
    recommended_next: {
      capability: "COLLECT_DIAGNOSTICS",
      summary: "Collect diagnostics",
      requires_approval: true,
      available: true,
      refs: [],
      summary_trust: "deterministic",
      summary_source: "platform",
    },
  };
}

let calls: string[];

beforeEach(() => {
  ({ calls } = stubFetch({
    "/api/t/acme/incidents": (url) => {
      const id = url.pathname.split("/")[5];
      if (!id) {
        return {
          body: {
            incidents: [incident("inc-1", "PSU 1 failed"), incident("inc-2", "Fan 3 degraded")],
            diagnosed: 0,
          },
        };
      }
      if (id === "inc-hidden") return { status: 404, body: { detail: "incident not found" } };
      return { body: detail(id, id === "inc-1" ? "PSU 1 failed" : "Fan 3 degraded") };
    },
  }));
});

const location = () => screen.getByTestId("location").textContent;

describe("an incident URL opens that incident", () => {
  it("a reload or a shared link opens the drawer, with the list's filter", async () => {
    renderAtRoute(<Incidents />, { route: "/t/acme/incidents/inc-1?status=resolved", path: PATH });
    const drawer = await screen.findByRole("dialog", { name: "PSU 1 failed" });
    expect(drawer).toBeTruthy();
    expect(calls).toContain("/api/t/acme/incidents?status=resolved");
    expect(calls).toContain("/api/t/acme/incidents/inc-1");
  });

  it("closing a drawer that was opened by link returns to the filtered list", async () => {
    const { user } = renderAtRoute(<Incidents />, {
      route: "/t/acme/incidents/inc-1?status=resolved",
      path: PATH,
    });
    await screen.findByRole("dialog", { name: "PSU 1 failed" });
    await user.click(screen.getByRole("button", { name: "Close panel" }));
    expect(location()).toBe("/t/acme/incidents?status=resolved");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("opening a row pushes its URL; Escape steps back to the exact list entry", async () => {
    const { user } = renderAtRoute(<Incidents />, { route: "/t/acme/incidents?status=all", path: PATH });
    await user.click(await screen.findByText("Fan 3 degraded"));
    expect(location()).toBe("/t/acme/incidents/inc-2?status=all");
    await screen.findByRole("dialog", { name: "Fan 3 degraded" });
    await user.keyboard("{Escape}");
    expect(location()).toBe("/t/acme/incidents?status=all");
  });

  it("a row opens from the keyboard too", async () => {
    const { user } = renderAtRoute(<Incidents />, { route: "/t/acme/incidents", path: PATH });
    await screen.findByText("PSU 1 failed");
    const row = screen.getByText("PSU 1 failed").closest("tr")!;
    row.focus();
    await user.keyboard("{Enter}");
    expect(location()).toBe("/t/acme/incidents/inc-1");
  });

  it("an incident that is absent or out of scope says so, and the drawer stays", async () => {
    renderAtRoute(<Incidents />, { route: "/t/acme/incidents/inc-hidden", path: PATH });
    const drawer = await screen.findByRole("dialog", { name: "Incident" });
    await waitFor(() =>
      expect(within(drawer).getByRole("status").textContent).toContain(
        "Not found, or not visible in your current scope",
      ),
    );
  });
});

describe("the status filter is the query string", () => {
  it("is read from the URL", async () => {
    renderAtRoute(<Incidents />, { route: "/t/acme/incidents?status=resolved", path: PATH });
    await screen.findByText("PSU 1 failed");
    expect(calls).toContain("/api/t/acme/incidents?status=resolved");
  });

  it("is written to the URL, and the default leaves the URL clean", async () => {
    const { user } = renderAtRoute(<Incidents />, { route: "/t/acme/incidents", path: PATH });
    await screen.findByText("PSU 1 failed");
    const select = screen.getByRole("combobox");
    await user.selectOptions(select, "resolved");
    expect(location()).toBe("/t/acme/incidents?status=resolved");
    await user.selectOptions(select, "open");
    expect(location()).toBe("/t/acme/incidents");
  });
});

describe("accessibility", () => {
  it("the page with an incident open has no axe violations", async () => {
    const { container } = renderAtRoute(<Incidents />, { route: "/t/acme/incidents/inc-1", path: PATH });
    await screen.findByRole("dialog", { name: "PSU 1 failed" });
    expect(await axeViolations(container)).toEqual([]);
  });
});
