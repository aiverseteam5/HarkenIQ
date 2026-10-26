// @vitest-environment jsdom
/** A34.4: Operational Agents and Campaigns open from their URLs.
 *
 *  The same contract Incidents and Fleet are held to, for the two other
 *  pages that had an object view: a link opens the object, a reload keeps
 *  it, closing returns to the list, and an object the server does not
 *  return is stated where the URL points rather than toasted away.
 */

import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import OperationalAgents from "./OperationalAgents";
import Campaigns from "./Campaigns";
import { renderAtRoute } from "../test/render";
import { stubFetch } from "../test/fetch";

const location = () => screen.getByTestId("location").textContent;

describe("an Operational Agent URL", () => {
  const PATH = "/t/:tenantId/operational-agents/:agentId?";

  it("a missing or out-of-scope agent is stated in the drawer the URL opened", async () => {
    const { calls } = stubFetch({
      "/api/t/acme/operational-agents/catalogue": { body: { action_classes: [], read_capabilities: [] } },
      "/api/t/acme/operational-agents/oa-hidden": { status: 404, body: { detail: "agent not found" } },
      "/api/t/acme/operational-agents/": { body: { agents: [] } },
      "/api/t/acme/sites": { body: { sites: [] } },
    });
    renderAtRoute(<OperationalAgents />, { route: "/t/acme/operational-agents/oa-hidden", path: PATH });
    const drawer = await screen.findByRole("dialog", { name: "Operational Agent" });
    await waitFor(() =>
      expect(within(drawer).getByRole("status").textContent).toContain("not visible in your current scope"),
    );
    expect(calls).toContain("/api/t/acme/operational-agents/oa-hidden");
  });

  it("closing a drawer opened by link returns to the list", async () => {
    stubFetch({
      "/api/t/acme/operational-agents/catalogue": { body: { action_classes: [], read_capabilities: [] } },
      "/api/t/acme/operational-agents/oa-hidden": { status: 404, body: { detail: "agent not found" } },
      "/api/t/acme/operational-agents/": { body: { agents: [] } },
      "/api/t/acme/sites": { body: { sites: [] } },
    });
    const { user } = renderAtRoute(<OperationalAgents />, {
      route: "/t/acme/operational-agents/oa-hidden",
      path: PATH,
    });
    await screen.findByRole("dialog", { name: "Operational Agent" });
    await user.click(screen.getByRole("button", { name: "Close panel" }));
    expect(location()).toBe("/t/acme/operational-agents");
  });
});

describe("a campaign URL", () => {
  const PATH = "/t/:tenantId/campaigns/:campaignId?";

  const CAMPAIGN = {
    id: "cmp-1",
    name: "Rack 7 LED sweep",
    description: "",
    action_type: "IDENTIFY_LED",
    status: "draft",
    version: 1,
    actor: "campaign:cmp-1",
    created_by: "ops@example.com",
    created_at: "2026-10-26T10:00:00Z",
    preflight_at: null,
    acknowledged_by: null,
    acknowledgement_valid: false,
    halt_reason: null,
    progress: {
      sites_total: 1,
      sites_completed: 0,
      sites_halted: 0,
      sites_running: 0,
      sites_pending: 1,
      targets_total: 0,
      targets_by_applicability: {},
      partial_success: false,
    },
  };

  function campaignRoutes(detailStatus = 200) {
    return {
      "/api/t/acme/campaigns/cmp-1/waves": { body: { waves: [] } },
      "/api/t/acme/campaigns/cmp-1":
        detailStatus === 200
          ? { body: { ...CAMPAIGN, sites: [], targets: [] } }
          : { status: detailStatus, body: { detail: "campaign not found" } },
      "/api/t/acme/campaigns/": { body: { campaigns: [CAMPAIGN] } },
    };
  }

  it("opens the campaign it names, with its waves", async () => {
    const { calls } = stubFetch(campaignRoutes());
    renderAtRoute(<Campaigns />, { route: "/t/acme/campaigns/cmp-1", path: PATH });
    await screen.findByRole("dialog", { name: "Rack 7 LED sweep" });
    expect(calls).toContain("/api/t/acme/campaigns/cmp-1");
    expect(calls).toContain("/api/t/acme/campaigns/cmp-1/waves");
  });

  it("a row opens its campaign URL", async () => {
    stubFetch(campaignRoutes());
    const { user } = renderAtRoute(<Campaigns />, { route: "/t/acme/campaigns", path: PATH });
    await user.click(await screen.findByText("Rack 7 LED sweep"));
    expect(location()).toBe("/t/acme/campaigns/cmp-1");
  });

  it("a refusal is the server's own words", async () => {
    stubFetch(campaignRoutes(403));
    renderAtRoute(<Campaigns />, { route: "/t/acme/campaigns/cmp-1", path: PATH });
    const drawer = await screen.findByRole("dialog", { name: "Campaign" });
    await waitFor(() =>
      expect(within(drawer).getByRole("status").textContent).toContain("campaign not found"),
    );
  });
});
