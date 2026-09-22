/** A30.29: the page renders the server's `withheld` flag, and copies
 *  nothing from a withheld block. */

import { describe, expect, it } from "vitest";
import {
  WITHHELD_NOTE,
  WITHHELD_YAML_NOTE,
  candidateGeneratedView,
  generatedView,
} from "./generatedContent";

const SECRET = "SECRET_SITE_C";

describe("a diagnosis block", () => {
  it("is rendered as sent when not withheld", () => {
    const view = generatedView({
      summary: "Fan bearing wear", suggested_action: "Replace fan",
      reasoning_steps: ["a", "b"], withheld: false,
    });
    expect(view).toEqual({
      withheld: false, summary: "Fan bearing wear", suggestedAction: "Replace fan",
      reasoningSteps: ["a", "b"], note: "",
    });
  });

  it("copies nothing from a withheld block, whatever it carries", () => {
    const view = generatedView({
      summary: `neutral sentence ${SECRET}`, suggested_action: SECRET,
      reasoning_steps: [SECRET], withheld: true,
    });
    expect(view.withheld).toBe(true);
    expect(view.note).toBe(WITHHELD_NOTE);
    expect(JSON.stringify(view)).not.toContain(SECRET);
  });

  it("treats a server that predates the flag as not withheld", () => {
    const view = generatedView({ summary: "x", suggested_action: "", reasoning_steps: [] });
    expect(view.withheld).toBe(false);
    expect(view.summary).toBe("x");
  });

  it("handles no block at all", () => {
    expect(generatedView(null).summary).toBe("");
  });
});

describe("a candidate's generated fields", () => {
  it("renders YAML and warnings when not withheld", () => {
    const view = candidateGeneratedView({
      yaml_text: "name: x", warnings: ["w"], generated_withheld: false,
    });
    expect(view).toEqual({ withheld: false, yamlText: "name: x", warnings: ["w"], note: "" });
  });

  it("copies neither YAML nor warnings when withheld", () => {
    const view = candidateGeneratedView({
      yaml_text: SECRET, warnings: [SECRET], generated_withheld: true,
    });
    expect(view).toEqual({ withheld: true, yamlText: "", warnings: [], note: WITHHELD_YAML_NOTE });
  });
});
