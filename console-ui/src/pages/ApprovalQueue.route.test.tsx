// @vitest-environment jsdom
/** A34.4: an approval is an address.
 *
 *  A28's measurement: "an incident commander cannot say 'look at this
 *  approval'. They can only say 'go to approvals and search'." There is no
 *  single-item read, so an item URL names its LANE and id, and the page
 *  finds it in the caller's own queue -- or says, without guessing which,
 *  why it is not there.
 */

import { beforeEach, describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import ApprovalQueue from "./ApprovalQueue";
import { renderAtRoute } from "../test/render";
import { stubFetch } from "../test/fetch";
import { links } from "../routes";

const PATH = "/t/:tenantId/approvals/:origin?/:itemId?";

function item(id: string, origin: "node" | "agent", actionType: string) {
  return {
    origin,
    id,
    site_id: "site-a",
    action_id: `act-${id}`,
    action_type: actionType,
    device_agent_id: "a1b2c3d4e5f60718",
    decision: null,
    decided_by: null,
    decided_at: null,
    routed_at: "2026-10-26T10:00:00Z",
    delivered_at: null,
  };
}

let calls: string[];

beforeEach(() => {
  ({ calls } = stubFetch({
    "/api/t/acme/approvals/history": { body: { actions: [], total: 0 } },
    "/api/t/acme/approvals": {
      body: {
        actions: [item("r-1", "node", "IDENTIFY_LED"), item("p-1", "agent", "COLLECT_DIAGNOSTICS")],
        total: 2,
      },
    },
  }));
});

const location = () => screen.getByTestId("location").textContent;

describe("an approval item URL", () => {
  it("focuses and marks the item it names", async () => {
    renderAtRoute(<ApprovalQueue />, { route: "/t/acme/approvals/agent/p-1", path: PATH });
    await waitFor(() => {
      const current = document.querySelector("[aria-current='true']");
      expect(current?.textContent).toContain("COLLECT_DIAGNOSTICS");
      expect(document.activeElement).toBe(current);
    });
  });

  it("names lane AND id: the same id in another lane is not this item", async () => {
    renderAtRoute(<ApprovalQueue />, { route: "/t/acme/approvals/node/p-1", path: PATH });
    expect(await screen.findByText(/not in the pending queue shown here/)).toBeTruthy();
    expect(document.querySelector("[aria-current='true']")).toBeNull();
  });

  it("an item that is not waiting says what that can mean, never which", async () => {
    renderAtRoute(<ApprovalQueue />, { route: "/t/acme/approvals/agent/p-decided", path: PATH });
    const notice = await screen.findByText(/not in the pending queue shown here/);
    expect(notice.textContent).toMatch(/decided.*not be visible in your current scope/s);
    expect(screen.getByRole("link", { name: "View history" }).getAttribute("href")).toBe(
      "/t/acme/approvals?tab=history",
    );
  });

  it("a lane outside the vocabulary is treated as not found", async () => {
    renderAtRoute(<ApprovalQueue />, { route: "/t/acme/approvals/bogus/r-1", path: PATH });
    expect(await screen.findByText(/not in the pending queue shown here/)).toBeTruthy();
  });

  it("every card links to itself", async () => {
    renderAtRoute(<ApprovalQueue />, { route: "/t/acme/approvals", path: PATH });
    const link = await screen.findByRole("link", { name: "Link to this COLLECT_DIAGNOSTICS approval" });
    expect(link.getAttribute("href")).toBe(links.approvalItem("acme", "agent", "p-1"));
  });

  it("lanes read in canonical terms", async () => {
    renderAtRoute(<ApprovalQueue />, { route: "/t/acme/approvals", path: PATH });
    expect(await screen.findByText("Operational Agent")).toBeTruthy();
    expect(screen.getByText("Harken Node")).toBeTruthy();
  });
});

describe("tab and filters are the query string", () => {
  it("?tab=history opens history", async () => {
    renderAtRoute(<ApprovalQueue />, { route: "/t/acme/approvals?tab=history", path: PATH });
    await waitFor(() => expect(calls.some((c) => c.startsWith("/api/t/acme/approvals/history?"))).toBe(true));
  });

  it("choosing a tab writes it, and the default leaves the URL clean", async () => {
    const { user } = renderAtRoute(<ApprovalQueue />, { route: "/t/acme/approvals", path: PATH });
    await screen.findByText("Harken Node");
    await user.click(screen.getByRole("button", { name: "History" }));
    expect(location()).toBe("/t/acme/approvals?tab=history");
    await user.click(screen.getByRole("button", { name: /Pending/ }));
    expect(location()).toBe("/t/acme/approvals");
  });

  it("a type filter in the URL is the filter the queue is read with", async () => {
    renderAtRoute(<ApprovalQueue />, { route: "/t/acme/approvals?type=POWER_CYCLE", path: PATH });
    await waitFor(() =>
      expect(calls).toContain("/api/t/acme/approvals?status=pending&type=POWER_CYCLE&page=1&page_size=100"),
    );
  });
});
