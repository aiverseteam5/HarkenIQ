/** A30.25 (D2): the Console never offers a contextual site as authority.
 *
 *  The server is the enforcement -- a contextual site is refused by every
 *  mutation. These tests hold the Console to not OFFERING it: the two
 *  authority surfaces filter it out, and only the explicit marker counts.
 */

import { describe, expect, it } from "vitest";
import {
  authoritativeSites,
  isContextualSite,
  type MaybeContextualSite,
} from "./siteContext";

/** A site row as the pages type it: the held shape, plus the marker. */
interface SiteRow extends MaybeContextualSite {
  id: string;
  site_name: string;
  sm_endpoint?: string;
  status?: string;
  org_unit_id?: string | null;
}

const held: SiteRow = {
  id: "site-a",
  site_name: "Site A",
  sm_endpoint: "sm:50051",
  status: "active",
  org_unit_id: null,
};
const contextual: SiteRow = {
  id: "site-b",
  site_name: "Site B",
  contextual: true,
};

describe("only the explicit marker makes a site contextual", () => {
  it("recognises the marker", () => {
    expect(isContextualSite(contextual)).toBe(true);
  });

  it("treats a row with no marker as held", () => {
    expect(isContextualSite(held)).toBe(false);
    expect("contextual" in held).toBe(false);
  });

  it("does not read a falsy or malformed marker as context", () => {
    expect(isContextualSite({ contextual: false })).toBe(false);
    expect(
      isContextualSite({ contextual: "true" } as unknown as {
        contextual?: boolean;
      }),
    ).toBe(false);
  });
});

describe("an authority surface offers held sites only", () => {
  it("drops contextual rows and keeps held ones untouched", () => {
    expect(authoritativeSites([held, contextual])).toEqual([held]);
  });

  it("a contextual row has no org unit, and must not read as unattached", () => {
    // The Organization page lists sites with no `org_unit_id` as
    // attachable. Filtering first is what keeps context out of that list.
    const unattached = authoritativeSites([held, contextual]).filter(
      (s) => !s.org_unit_id,
    );
    expect(unattached.map((s) => s.id)).toEqual(["site-a"]);
  });

  it("is the identity for a caller who holds every site they see", () => {
    expect(authoritativeSites([held])).toEqual([held]);
    expect(authoritativeSites([])).toEqual([]);
  });
});
