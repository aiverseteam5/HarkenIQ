/** A34.5: a closed vocabulary, read the way the server reads it.
 *
 *  Every view-model in this directory does the same thing to a value that
 *  arrives over the wire: it accepts exactly the server's spelling of a
 *  known value and turns ANYTHING else -- a missing key, a null, an empty
 *  string, a value a newer server added, a hand-edited row -- into one
 *  first-class `unknown` (A28.6 P3). `proposalProvenance` did it by hand
 *  first (A28.12's reference shape); this is the same rule, made reusable,
 *  so the next module cannot get it subtly different.
 *
 *  Presentation only: knowing that a value is one of a set answers no
 *  authorization question.
 */

export const UNKNOWN = "unknown" as const;
export type Unknown = typeof UNKNOWN;

export interface ClosedVocabulary<V extends string> {
  /** The known values, in the server's spelling. */
  readonly values: readonly V[];
  /** True only for a known value; never for `unknown` itself unless the
   *  vocabulary lists it. */
  has(raw: unknown): raw is V;
  /** The value, or `unknown` for anything the vocabulary does not name. */
  read(raw: unknown): V | Unknown;
}

export function closedVocabulary<V extends string>(
  values: readonly V[],
): ClosedVocabulary<V> {
  const known: ReadonlySet<string> = new Set(values);
  const has = (raw: unknown): raw is V =>
    typeof raw === "string" && known.has(raw);
  return {
    values,
    has,
    read: (raw) => (has(raw) ? raw : UNKNOWN),
  };
}
