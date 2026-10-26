/** A30.39 (S3-E2): a proposal's track record, as a human reads it.
 *
 *  TWO RECORDS, TWO NAMES
 *  ----------------------
 *  A stored proposal carries the outcome record its decision was CREATED
 *  with -- composed across the whole tenant, and immutable. Beside it,
 *  Central Command now sends `viewer_projected_evidence`: the reader's
 *  CURRENT track record, over exactly the outcomes their current scope
 *  reads. They are never the same number and this module never lets one
 *  stand in for the other.
 *
 *  WHY A WITHHELD RECORD IS NOT "TOO FEW OUTCOMES"
 *  -----------------------------------------------
 *  For a reader whose scope is narrower than the tenant, the server
 *  withholds the creation record (`outcome_evidence: null`). The queue used
 *  to read that null as "too few outcomes to judge" -- a claim about the
 *  estate nobody made. A withheld record is WITHHELD, and says so.
 *
 *  THE READING RULE IS THE SERVER'S
 *  --------------------------------
 *  Only what the server states is rendered. An absent or unrecognised
 *  viewer block reads as unavailable -- never as zero, which would say
 *  "no executions" about a view nobody computed. Presentation only: it
 *  answers no authorization question and is never an input to one.
 */

/** The outcome counts the server sends (`_evidence_for`'s keys). */
export interface OutcomeEvidence {
  executions: number;
  success?: number;
  failure?: number;
  success_rate: number | null;
  resolution_rate?: number | null;
  sites_observed?: number;
  sufficient: boolean;
  window?: string;
}

/** `viewer_projected_evidence`, as it arrives over the wire. */
export interface ViewerProjectedEvidence {
  basis: string;
  as_of?: string;
  outcome_evidence: OutcomeEvidence | null;
  unavailable_reason: string | null;
}

/** The fields of a proposal this module reads. Typed as `unknown`-tolerant
 *  as the network requires: a Central Command predating S3-E2 sends none of
 *  the new ones. */
export interface ProposalEvidenceFields {
  evidence?: { outcome_evidence?: OutcomeEvidence | null } | null;
  evidence_scope?: string;
  viewer_projected_evidence?: ViewerProjectedEvidence | null;
}

export const EVIDENCE_SCOPES = ["fully_visible", "broader_than_current_view"] as const;

/** What the page may render. Nothing else escapes this module. */
export interface TrackRecordView {
  /** The reader's CURRENT track record, in words. */
  current: string;
  /** The creation record, ONLY for a reader who may read it whole. */
  atCreation: string | null;
  /** The bounded note for a reader whose scope is narrower than the
   *  creation record's; `null` otherwise. */
  note: string | null;
}

export const OUTSIDE_SCOPE_NOTE =
  "Creation evidence outside your current scope is not shown.";
export const UNAVAILABLE = "unavailable";
export const NO_REACH =
  "not available: you hold no fleet visibility where this runs";

function isEvidence(value: unknown): value is OutcomeEvidence {
  if (value === null || typeof value !== "object") return false;
  const v = value as Record<string, unknown>;
  return typeof v.executions === "number" && Number.isFinite(v.executions)
    && v.executions >= 0 && typeof v.sufficient === "boolean";
}

/** One outcome record in words, qualified by where it was counted. */
export function describeRecord(evidence: OutcomeEvidence, where: string): string {
  const runs = evidence.executions;
  if (
    evidence.sufficient &&
    typeof evidence.success_rate === "number" &&
    Number.isFinite(evidence.success_rate)
  ) {
    return `${Math.round(evidence.success_rate * 100)}% over ${runs} runs ${where}`;
  }
  if (runs > 0) {
    return `${runs} run(s) ${where}: too few to judge`;
  }
  return `no runs ${where} yet`;
}

/** The one reading rule, mirroring the server's. */
export function trackRecordView(
  proposal?: ProposalEvidenceFields | null,
): TrackRecordView {
  const viewer = proposal?.viewer_projected_evidence;
  let current = UNAVAILABLE;
  if (viewer && viewer.basis === "current_reach") {
    if (isEvidence(viewer.outcome_evidence)) {
      current = describeRecord(viewer.outcome_evidence, "in your current view");
    } else if (viewer.unavailable_reason === "no_fleet_view_reach") {
      current = NO_REACH;
    }
  }

  const scope = proposal?.evidence_scope;
  if (scope === "fully_visible") {
    const created = proposal?.evidence?.outcome_evidence;
    return {
      current,
      atCreation: isEvidence(created)
        ? describeRecord(created, "across the tenant when proposed")
        : null,
      note: null,
    };
  }
  // `broader_than_current_view`, and anything unrecognised: the creation
  // record is never rendered, and the note says it exists without saying
  // anything about it.
  return {
    current,
    atCreation: null,
    note: scope === "broader_than_current_view" ? OUTSIDE_SCOPE_NOTE : null,
  };
}
