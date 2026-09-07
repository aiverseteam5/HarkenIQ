/** A27.6: the two questions stay two questions, on the human surfaces.
 *
 *  The backend separated proposal PROVENANCE from the approval queue's
 *  LANE. These tests hold the Console to the same separation, and hold
 *  the presentation rule to the server's own: anything unrecognised is
 *  `unknown`, and a submission id rides only with an external
 *  submission.
 */

import { describe, expect, it } from "vitest";
import {
  PROVENANCE_TYPES,
  provenanceView,
  type ProposalProvenance,
} from "./proposalProvenance";
import { ORIGIN_LABEL } from "./pages/ApprovalQueue";

describe("the three sources render distinctly", () => {
  it("names HarkenIQ's own evaluator", () => {
    expect(provenanceView({ type: "evaluator" })).toEqual({
      type: "evaluator",
      label: "HarkenIQ evaluator",
      submissionId: "",
    });
  });

  it("names an external agent runtime", () => {
    expect(provenanceView({ type: "external_ingress" }).label).toBe(
      "External agent runtime",
    );
  });

  it("names a historical row as unknown, never as the evaluator", () => {
    expect(provenanceView({ type: "unknown" }).label).toBe(
      "Unknown historical source",
    );
  });

  it("gives every source its own words", () => {
    const labels = PROVENANCE_TYPES.map((t) => provenanceView({ type: t }).label);
    expect(new Set(labels).size).toBe(PROVENANCE_TYPES.length);
  });
});

describe("absent or unrecognised provenance is unknown, never guessed", () => {
  // A27.4: a proposal created before the column existed has no
  // authoritative provenance. Presenting it as `evaluator` would assert
  // a fact nobody checked.
  const noProvenance: (ProposalProvenance | null | undefined)[] = [
    undefined,
    null,
    { type: "" },
    { type: "ingress" }, // the pre-A27 spelling, deliberately not aliased
    { type: "EVALUATOR" }, // case is not a synonym
    { type: "webhook" }, // a source that does not exist yet
    { type: "<script>alert(1)</script>" },
  ];

  for (const provenance of noProvenance) {
    it(`reads unknown for ${JSON.stringify(provenance)}`, () => {
      const view = provenanceView(provenance);
      expect(view.type).toBe("unknown");
      expect(view.label).toBe("Unknown historical source");
    });
  }

  it("never renders an unvetted server string", () => {
    const view = provenanceView({ type: "<script>alert(1)</script>" });
    expect(view.label).not.toContain("script");
  });
});

describe("the submission id rides ONLY with an external submission", () => {
  it("is carried for an external submission", () => {
    expect(
      provenanceView({ type: "external_ingress", submission_id: "sub-1" })
        .submissionId,
    ).toBe("sub-1");
  });

  it("is withheld for an evaluator proposal even if the server sends one", () => {
    // Judged on the TYPE, not on whether the field happens to be
    // present: the rule is "external submissions have a submission",
    // not "render whatever arrived".
    expect(
      provenanceView({ type: "evaluator", submission_id: "sub-1" })
        .submissionId,
    ).toBe("");
  });

  it("is withheld for an unknown proposal even if the server sends one", () => {
    expect(
      provenanceView({ type: "unknown", submission_id: "sub-1" }).submissionId,
    ).toBe("");
  });

  it("is withheld for an unrecognised type carrying one", () => {
    expect(
      provenanceView({ type: "ingress", submission_id: "sub-1" }).submissionId,
    ).toBe("");
  });

  it("is empty, not undefined, when the server omits it", () => {
    expect(provenanceView({ type: "external_ingress" }).submissionId).toBe("");
  });
});

describe("the approval queue LANE is untouched", () => {
  // A27.2: `origin` answers which queue lane a subject belongs to.
  // Provenance answers who caused it. Merging them is the defect A6-3
  // fixed, so this asserts they cannot have been merged since.
  it("still maps exactly the three lanes it always did", () => {
    expect(ORIGIN_LABEL).toEqual({
      node: "node",
      agent: "agent",
      agent_activation: "agent activation",
    });
  });

  it("has no lane value in the provenance vocabulary", () => {
    for (const lane of Object.keys(ORIGIN_LABEL)) {
      expect(PROVENANCE_TYPES as readonly string[]).not.toContain(lane);
    }
  });

  it("never presents provenance as a lane", () => {
    const laneWords = new Set(Object.values(ORIGIN_LABEL));
    for (const type of PROVENANCE_TYPES) {
      expect(laneWords.has(provenanceView({ type }).label)).toBe(false);
    }
  });

  it("leaves an external proposal in the `agent` lane", () => {
    // The lane is a property of the queue item; provenance is a property
    // of the proposal. An external submission is still an agent-lane
    // subject, and reading one must not change the other.
    const item = {
      origin: "agent" as const,
      proposal: { provenance: { type: "external_ingress" } },
    };
    expect(ORIGIN_LABEL[item.origin]).toBe("agent");
    expect(provenanceView(item.proposal.provenance).label).toBe(
      "External agent runtime",
    );
  });
});

describe("provenance exposes no authorization internals", () => {
  it("returns exactly three fields, whatever the server sends", () => {
    const view = provenanceView({
      type: "external_ingress",
      submission_id: "sub-1",
      // Anything else on the wire is not part of the contract and must
      // not reach a page by travelling through this module.
      ...({ client_id: "kc-client", realm: "tenant-demo" } as object),
    });
    expect(Object.keys(view).sort()).toEqual([
      "label",
      "submissionId",
      "type",
    ]);
    expect(JSON.stringify(view)).not.toContain("kc-client");
    expect(JSON.stringify(view)).not.toContain("tenant-demo");
  });
});
