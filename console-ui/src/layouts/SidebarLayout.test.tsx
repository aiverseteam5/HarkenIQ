// @vitest-environment jsdom
/** A34.6 + A34.8: the shell -- its context, its landmarks, its focus.
 *
 *  Rendered with stand-in pages, so these tests are about what every page
 *  inherits: a skip link, real navigation links, a title per page, focus
 *  that moves when the PAGE changes (and only then), and the operational
 *  context stated from Central Command's own contracts -- never requested
 *  for a platform identity, which Central Command would refuse with a 401
 *  that signs the user out.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import SidebarLayout from "./SidebarLayout";
import { LocationProbe, renderWithUser } from "../test/render";
import { stubFetch } from "../test/fetch";
import { axeViolations } from "../test/axe";
import type { AuthUser } from "../useAuth";

const auth = vi.hoisted(() => ({ user: null as AuthUser | null }));

vi.mock("../useAuth", () => ({
  useAuth: () => ({
    user: auth.user,
    isAuthenticated: auth.user !== null,
    loading: false,
    login: () => {},
    logout: () => {},
    completeLogin: async () => {},
  }),
}));

const TENANT_USER: AuthUser = {
  email: "ops@acme.example",
  name: "Ops Lead",
  role: "operator",
  tenant_id: "acme",
  permissions: ["fleet.view", "incident.view", "action.approve"],
  is_platform_user: false,
};

const PLATFORM_USER: AuthUser = {
  ...TENANT_USER,
  email: "support@harkeniq.example",
  role: "platform_support",
  tenant_id: "",
  permissions: ["tenant.view", "fleet.view", "incident.view"],
  is_platform_user: true,
};

function Page({ title }: { title: string }) {
  return (
    <>
      <h1>{title}</h1>
      <Link to="/t/acme/incidents/inc-1">Open incident</Link>
    </>
  );
}

function shellAt(route: string) {
  return renderWithUser(
    <MemoryRouter initialEntries={[route]}>
      <Routes>
        <Route element={<SidebarLayout />}>
          <Route path="/t/:tenantId/incidents/:incidentId?" element={<Page title="Incidents & Diagnosis" />} />
          <Route path="/t/:tenantId/fleet/:agentId?" element={<Page title="Fleet Overview" />} />
        </Route>
      </Routes>
      <LocationProbe />
    </MemoryRouter>,
  );
}

let calls: string[];

beforeEach(() => {
  auth.user = TENANT_USER;
  ({ calls } = stubFetch({
    "/api/me/tenants": { body: { tenants: [{ id: "acme", name: "Acme Corp", slug: "acme" }] } },
    "/api/admin/tenants/acme": { body: { id: "acme", name: "Acme Corp", slug: "acme" } },
    "/api/t/acme/scope-grants/me": {
      body: { enforcement: "strict", tenant_wide: false, site_ids: ["site-a"], synthesis: "granted" },
    },
    "/api/t/acme/autonomy/": {
      body: {
        generated_at: "2026-10-26T12:00:00Z",
        posture: { stop_switch: { active: true, changed_at: null } },
        safety_state: {
          reported: true,
          every_site_reported: true,
          sites_not_reporting: [],
          site_stop_switches: [],
          suppressions: [],
        },
        scope: { sites: [{ id: "site-a", name: "Pune DC1" }] },
      },
    },
  }));
});

describe("the operational context, stated from Central Command's contracts", () => {
  it("names tenant, scope, posture, safety and freshness", async () => {
    shellAt("/t/acme/incidents");
    const bar = screen.getByRole("region", { name: "Operational context" });
    await within(bar).findByText("Acme Corp");
    await within(bar).findByText("Stop switch ON");
    expect(within(bar).getByText("1 site")).toBeTruthy();
    expect(within(bar).getByText("Strict")).toBeTruthy();
    expect(within(bar).getByText(/^Updated /)).toBeTruthy();
    expect(calls).toContain("/api/t/acme/scope-grants/me");
    expect(calls).toContain("/api/t/acme/autonomy/");
  });

  it("never asks Central Command about a platform identity", async () => {
    auth.user = PLATFORM_USER;
    shellAt("/t/acme/incidents");
    const bar = screen.getByRole("region", { name: "Operational context" });
    await within(bar).findByText("Acme Corp");
    // D4 (A35.2): the bar names the plane; the product is HarkenIQ Console.
    expect(within(bar).getByText("Platform")).toBeTruthy();
    expect(calls.filter((c) => c.startsWith("/api/t/"))).toEqual([]);
  });

  it("the context sits before main, so skipping to main skips it", () => {
    shellAt("/t/acme/incidents");
    const bar = screen.getByRole("region", { name: "Operational context" });
    expect(screen.getByRole("main").contains(bar)).toBe(false);
  });
});

describe("landmarks, skip link and navigation", () => {
  it("the first Tab reaches the skip link, which moves focus to main", async () => {
    const { user } = shellAt("/t/acme/incidents");
    await user.tab();
    const skip = screen.getByRole("link", { name: "Skip to main content" });
    expect(document.activeElement).toBe(skip);
    await user.keyboard("{Enter}");
    expect(document.activeElement).toBe(screen.getByRole("main"));
  });

  it("sidebar entries are real links into the current tenant", () => {
    shellAt("/t/acme/incidents");
    const nav = screen.getByRole("navigation", { name: "Main navigation" });
    expect(within(nav).getByRole("link", { name: "Incidents" }).getAttribute("href")).toBe("/t/acme/incidents");
    expect(within(nav).getByRole("link", { name: "Fleet Overview" }).getAttribute("href")).toBe("/t/acme/fleet");
    expect(within(nav).getByRole("link", { name: "Incidents" }).getAttribute("aria-current")).toBe("page");
  });

  it("the shell has no axe violations", async () => {
    const { container } = shellAt("/t/acme/incidents");
    await screen.findByText("Stop switch ON");
    expect(await axeViolations(container, { page: true })).toEqual([]);
  });
});

describe("arriving on a page", () => {
  it("names the page in the document title", async () => {
    shellAt("/t/acme/incidents/inc-1");
    await waitFor(() => expect(document.title).toBe("Incidents & Diagnosis · HarkenIQ Console"));
  });

  it("moves focus to the new page's heading when the page changes", async () => {
    const { user } = shellAt("/t/acme/incidents");
    const nav = screen.getByRole("navigation", { name: "Main navigation" });
    await user.click(within(nav).getByRole("link", { name: "Fleet Overview" }));
    expect(screen.getByTestId("location").textContent).toBe("/t/acme/fleet");
    expect(document.activeElement).toBe(screen.getByRole("heading", { level: 1, name: "Fleet Overview" }));
    expect(document.title).toBe("Fleet Overview · HarkenIQ Console");
  });

  it("does not move focus when an object opens on the same page", async () => {
    const { user } = shellAt("/t/acme/incidents");
    const link = screen.getByRole("link", { name: "Open incident" });
    await user.click(link);
    expect(screen.getByTestId("location").textContent).toBe("/t/acme/incidents/inc-1");
    expect(document.activeElement).toBe(screen.getByRole("link", { name: "Open incident" }));
  });

  it("does not move focus on the first render, so the skip link is still first", () => {
    shellAt("/t/acme/incidents");
    expect(document.activeElement).toBe(document.body);
  });
});
