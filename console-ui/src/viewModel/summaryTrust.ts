/** A30.34 (D6): where an incident's "what should happen next" came from.
 *
 *  Central Command's incident detail can quote the reasoning model's
 *  `suggested_action` as `recommended_next.summary`, and this page renders
 *  that summary in bold -- so model text read exactly like HarkenIQ's own
 *  recommendation (F4). The server now says where the sentence came from
 *  (`summary_trust`, `summary_source`); this module is how the page reads
 *  that, and the page renders the result beside the summary.
 *
 *  It applies the SERVER's rule rather than reaching its own verdict: the
 *  four trust classes are the server's closed vocabulary, and anything else
 *  -- a missing field from an older Central Command, an unrecognised word --
 *  is shown as an unstated source, never as HarkenIQ's. Presentation only;
 *  it decides nothing and is never an input to a decision.
 */

/** The server's closed trust vocabulary (harkeniq_cc.trust). */
export const TRUST_CLASSES = [
  "deterministic",
  "operator_supplied",
  "untrusted_telemetry",
  "untrusted_generated",
] as const;

export type TrustClass = (typeof TRUST_CLASSES)[number];

/** Where the summary text itself came from. */
export const SUMMARY_SOURCE_PLATFORM = "platform";
export const SUMMARY_SOURCE_GENERATED = "diagnosis.generated.suggested_action";

/** The two fields the server sends beside `summary`. Typed as strings:
 *  they arrive over the wire, and `summarySourceView` is what makes them
 *  safe to render. */
export interface SummaryProvenance {
  summary_trust?: string;
  summary_source?: string;
}

export interface SummarySourceView {
  /** The recognised class, or "unknown" for anything the server did not
   *  state in its own vocabulary. */
  trust: TrustClass | "unknown";
  /** The summary quotes the reasoning model's suggestion. */
  generated: boolean;
  /** Render as a caution rather than as a plain attribution. */
  caution: boolean;
  /** The sentence the page shows beside the summary. */
  label: string;
}

export const LABELS: Record<TrustClass | "unknown", string> = {
  deterministic: "Source: HarkenIQ.",
  operator_supplied: "Source: text an operator entered.",
  untrusted_telemetry:
    "Source: text reported by the device or its controller. Treat it as unverified.",
  untrusted_generated:
    "Source: the reasoning model, written from device telemetry. This is not " +
    "a HarkenIQ recommendation; a named human decides whether anything happens.",
  unknown:
    "Source not stated by Central Command. Treat this text as unverified.",
};

function isTrustClass(value: unknown): value is TrustClass {
  return (
    typeof value === "string" &&
    (TRUST_CLASSES as readonly string[]).includes(value)
  );
}

/** The one reading rule. */
export function summarySourceView(
  rec?: SummaryProvenance | null,
): SummarySourceView {
  const raw = rec?.summary_trust;
  const generated = rec?.summary_source === SUMMARY_SOURCE_GENERATED;
  let trust: TrustClass | "unknown" = isTrustClass(raw) ? raw : "unknown";
  // The server's derivation never labels a quoted model suggestion as
  // HarkenIQ's or an operator's. A message that does contradicts itself,
  // and a contradiction is unverified -- never the better of the two.
  if (generated && (trust === "deterministic" || trust === "operator_supplied")) {
    trust = "unknown";
  }
  return {
    trust,
    generated,
    caution: trust !== "deterministic" && trust !== "operator_supplied",
    label: LABELS[trust],
  };
}
