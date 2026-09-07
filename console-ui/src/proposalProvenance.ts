/** A27.6 (A6-3): who caused a proposal to exist, as a human reads it.
 *
 *  WHY THIS IS NOT THE APPROVAL QUEUE'S `origin`
 *  ---------------------------------------------
 *  `ApprovalAction.origin` answers WHICH QUEUE LANE a subject belongs to
 *  -- `node`, `agent`, `agent_activation`. Provenance answers WHO CAUSED
 *  IT. Central Command separated them precisely because one word
 *  answering both is how the queue came to say "agent" for a proposal
 *  HarkenIQ reasoned itself AND for one an external runtime asked for.
 *  Nothing here may be used as a lane, and nothing here changes a lane.
 *
 *  WHY THE VOCABULARY IS CLOSED
 *  ----------------------------
 *  The server's own reading rule (`provenance_type`) maps anything it
 *  does not recognise to `unknown` rather than reporting it verbatim.
 *  This module applies the SAME rule so the two cannot disagree: a
 *  future writer, a partial rollout, or a hand-edited row cannot put an
 *  unvetted string in front of an approver.
 *
 *  It is presentation only. It answers no authorization question, and it
 *  is never an input to one -- an externally submitted proposal is
 *  governed identically to an internally derived one.
 */

/** The closed vocabulary, in the server's spelling. */
export const PROVENANCE_TYPES = [
  "evaluator",
  "external_ingress",
  "unknown",
] as const;

export type ProvenanceType = (typeof PROVENANCE_TYPES)[number];

/** The block Central Command sends beside a proposal.
 *
 *  `type` is deliberately typed as `string`: it arrives over the wire
 *  and a narrower type here would be a promise the network cannot keep.
 *  `provenanceView` is what turns it into something safe to render. */
export interface ProposalProvenance {
  type: string;
  /** A27.6: present ONLY for an external submission. Bounded correlation
   *  detail so an operator can tie a decision to the submission that
   *  asked for it -- never a credential, and it confers nothing. */
  submission_id?: string;
}

const LABEL: Record<ProvenanceType, string> = {
  evaluator: "HarkenIQ evaluator",
  external_ingress: "External agent runtime",
  unknown: "Unknown historical source",
};

/** What the page may render. Nothing else escapes this module. */
export interface ProvenanceView {
  type: ProvenanceType;
  label: string;
  /** Empty unless the proposal is genuinely external. Never rendered
   *  for anything else, whatever the server happened to send. */
  submissionId: string;
}

function isProvenanceType(value: unknown): value is ProvenanceType {
  return (
    typeof value === "string" &&
    (PROVENANCE_TYPES as readonly string[]).includes(value)
  );
}

/** The one reading rule, mirroring the server's.
 *
 *  A missing block, a null, an empty string and an unrecognised value
 *  are all exactly as unknown as each other. A pre-A6-3 proposal has no
 *  authoritative provenance and says so; it is never presented as though
 *  HarkenIQ's own evaluator produced it (A27.4). */
export function provenanceView(
  provenance?: ProposalProvenance | null,
): ProvenanceView {
  const raw = provenance?.type;
  const type: ProvenanceType = isProvenanceType(raw) ? raw : "unknown";
  const submissionId =
    type === "external_ingress" ? (provenance?.submission_id ?? "") : "";
  return { type, label: LABEL[type], submissionId };
}
