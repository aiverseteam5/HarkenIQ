/** A30.39 (S3-E2): the track record a human reads, held to the server's rule.
 *
 *  Two records, two names: the reader's CURRENT record (always the one the
 *  "Track record" row shows) and the CREATION record (shown only to a
 *  reader who may read it whole). A withheld creation record is never
 *  rendered as "too few outcomes", and nothing the server did not state is
 *  rendered as zero.
 */

import { describe, expect, it } from "vitest";
import {
  NO_REACH,
  OUTSIDE_SCOPE_NOTE,
  UNAVAILABLE,
  trackRecordView,
  type OutcomeEvidence,
  type ProposalEvidenceFields,
} from "./proposalEvidence";

const HIDDEN = 918273645;

const created: OutcomeEvidence = {
  executions: 40, success: 23, failure: 17, success_rate: 0.575,
  sufficient: true,
};

function viewer(outcome: OutcomeEvidence | null, reason: string | null = null) {
  return {
    basis: "current_reach", as_of: "2026-09-30T00:00:00+00:00",
    outcome_evidence: outcome, unavailable_reason: reason,
  };
}

describe("a scoped reader", () => {
  const proposal: ProposalEvidenceFields = {
    evidence: { outcome_evidence: null },
    evidence_scope: "broader_than_current_view",
    viewer_projected_evidence: viewer({
      executions: 11, success: 10, failure: 1, success_rate: 0.9091, sufficient: true,
    }),
  };

  it("reads their own current record", () => {
    expect(trackRecordView(proposal).current).toBe("91% over 11 runs in your current view");
  });

  it("is told the creation record exists outside their scope, and nothing more", () => {
    const view = trackRecordView(proposal);
    expect(view.note).toBe(OUTSIDE_SCOPE_NOTE);
    expect(view.atCreation).toBeNull();
  });

  it("never reads a withheld record as 'too few outcomes'", () => {
    const text = JSON.stringify(trackRecordView(proposal));
    expect(text).not.toContain("too few outcomes");
    expect(text).not.toContain("40");
  });

  it("never renders a creation record the server should have withheld", () => {
    // Defence in depth: even if a stored statistic arrived beside a scoped
    // label, the scoped rule does not render it.
    const leaky = { ...proposal, evidence: { outcome_evidence: { ...created, executions: HIDDEN } } };
    expect(JSON.stringify(trackRecordView(leaky))).not.toContain(String(HIDDEN));
  });
});

describe("a tenant-wide reader", () => {
  const proposal: ProposalEvidenceFields = {
    evidence: { outcome_evidence: created },
    evidence_scope: "fully_visible",
    viewer_projected_evidence: viewer({ ...created, executions: 100, success_rate: 0.83 }),
  };

  it("reads the current record AND, separately, the record it was proposed with", () => {
    const view = trackRecordView(proposal);
    expect(view.current).toBe("83% over 100 runs in your current view");
    expect(view.atCreation).toBe("57% over 40 runs across the tenant when proposed");
    expect(view.note).toBeNull();
  });

  it("gives the two records different words", () => {
    const view = trackRecordView(proposal);
    expect(view.current).not.toBe(view.atCreation);
  });
});

describe("what the server did not state is not a number", () => {
  it("an approver with no fleet visibility reads no track record, not zero", () => {
    const view = trackRecordView({
      evidence_scope: "broader_than_current_view",
      viewer_projected_evidence: viewer(null, "no_fleet_view_reach"),
    });
    expect(view.current).toBe(NO_REACH);
    expect(view.current).not.toMatch(/\b0\b/);
  });

  const absent: (ProposalEvidenceFields | null | undefined)[] = [
    undefined,
    null,
    {},
    { viewer_projected_evidence: null },
    { viewer_projected_evidence: { basis: "tenant", outcome_evidence: created, unavailable_reason: null } },
    { viewer_projected_evidence: viewer({ executions: -1, success_rate: null, sufficient: false }) },
    { viewer_projected_evidence: viewer(null, "some_future_reason") },
  ];
  for (const proposal of absent) {
    it(`reads unavailable for ${JSON.stringify(proposal)}`, () => {
      expect(trackRecordView(proposal).current).toBe(UNAVAILABLE);
    });
  }

  it("an unrecognised scope renders no creation record and no note", () => {
    const view = trackRecordView({
      evidence: { outcome_evidence: created },
      evidence_scope: "everything",
      viewer_projected_evidence: viewer(created),
    });
    expect(view.atCreation).toBeNull();
    expect(view.note).toBeNull();
  });
});

describe("small records are described honestly", () => {
  it("too few runs in the reader's own view", () => {
    const view = trackRecordView({
      evidence_scope: "broader_than_current_view",
      viewer_projected_evidence: viewer({ executions: 3, success_rate: null, sufficient: false }),
    });
    expect(view.current).toBe("3 run(s) in your current view: too few to judge");
  });

  it("no runs yet in the reader's own view", () => {
    const view = trackRecordView({
      evidence_scope: "broader_than_current_view",
      viewer_projected_evidence: viewer({ executions: 0, success_rate: null, sufficient: false }),
    });
    expect(view.current).toBe("no runs in your current view yet");
  });
});
