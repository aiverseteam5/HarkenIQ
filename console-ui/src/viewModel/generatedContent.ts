/** A30.29: generated content, as a human may read it.
 *
 *  An incident's diagnosis text and a candidate skill's YAML were written
 *  by a model from a prompt that carried whichever fleet-pattern payload
 *  the Site Manager held. Central Command records the projection boundary
 *  that payload had (`generation_visibility`) and shows the generated
 *  fields only to a reader whose CURRENT reach covers it; everyone else is
 *  sent a block whose `withheld` flag is true and whose text is one
 *  neutral sentence.
 *
 *  This module applies the SERVER's reading of that flag, so the page
 *  cannot present a withheld block as a diagnosis (or an empty YAML as
 *  "no behaviour proposed"). It is presentation only: it decides nothing,
 *  and a page that reached its own verdict about what a reader may see
 *  would be a second rule.
 */

/** The `diagnosis.generated` block Central Command sends. `withheld` is
 *  typed optional because a pre-A30.29 Central Command does not send it;
 *  absent reads as NOT withheld, which is exactly what such a server did. */
export interface GeneratedBlock {
  summary: string;
  suggested_action: string;
  reasoning_steps: string[];
  withheld?: boolean;
}

/** A candidate row's generated fields, same rule. */
export interface CandidateGenerated {
  yaml_text: string;
  warnings: string[];
  generated_withheld?: boolean;
}

export const WITHHELD_NOTE =
  "Withheld: this text was generated from fleet evidence whose projection " +
  "is outside your authorized scope, or was recorded without one. The " +
  "incident's own facts above are unaffected.";

export const WITHHELD_YAML_NOTE =
  "Withheld: this candidate's behaviour was generated from fleet evidence " +
  "whose projection is outside your authorized scope, or was recorded " +
  "without one.";

export interface GeneratedView {
  withheld: boolean;
  summary: string;
  suggestedAction: string;
  reasoningSteps: string[];
  /** Set only when withheld; the page renders it in place of the text. */
  note: string;
}

export function generatedView(block?: GeneratedBlock | null): GeneratedView {
  if (!block) {
    return { withheld: false, summary: "", suggestedAction: "", reasoningSteps: [], note: "" };
  }
  if (block.withheld === true) {
    // Nothing from the block is carried: the server sent a neutral
    // sentence, and the page says why in its own words.
    return { withheld: true, summary: "", suggestedAction: "", reasoningSteps: [], note: WITHHELD_NOTE };
  }
  return {
    withheld: false,
    summary: block.summary ?? "",
    suggestedAction: block.suggested_action ?? "",
    reasoningSteps: Array.isArray(block.reasoning_steps) ? block.reasoning_steps : [],
    note: "",
  };
}

export interface CandidateView {
  withheld: boolean;
  yamlText: string;
  warnings: string[];
  note: string;
}

export function candidateGeneratedView(row?: CandidateGenerated | null): CandidateView {
  if (!row) {
    return { withheld: false, yamlText: "", warnings: [], note: "" };
  }
  if (row.generated_withheld === true) {
    return { withheld: true, yamlText: "", warnings: [], note: WITHHELD_YAML_NOTE };
  }
  return {
    withheld: false,
    yamlText: row.yaml_text ?? "",
    warnings: Array.isArray(row.warnings) ? row.warnings : [],
    note: "",
  };
}
