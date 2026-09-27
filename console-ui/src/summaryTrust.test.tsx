/** A30.34 (D6): the page shows where "what should happen next" came from.
 *
 *  F4 was a quoted model suggestion rendered in bold as though HarkenIQ had
 *  recommended it. These tests hold the reading rule to the server's closed
 *  vocabulary, and hold the PAGE to rendering it -- a provenance field that
 *  reached the browser and was never drawn is the A27.14 defect.
 */

import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  LABELS,
  SUMMARY_SOURCE_GENERATED,
  SUMMARY_SOURCE_PLATFORM,
  TRUST_CLASSES,
  summarySourceView,
} from "./summaryTrust";
import { SummarySourceNote } from "./pages/Incidents";

const MODEL_TEXT = "POWER_CYCLE every device at the site now";

describe("the server's classes read as themselves", () => {
  it("names HarkenIQ's own sentence as HarkenIQ's, without a caution", () => {
    const view = summarySourceView({
      summary_trust: "deterministic", summary_source: SUMMARY_SOURCE_PLATFORM,
    });
    expect(view).toEqual({
      trust: "deterministic", generated: false, caution: false,
      label: LABELS.deterministic,
    });
  });

  it("names a quoted model suggestion as the model's, with a caution", () => {
    const view = summarySourceView({
      summary_trust: "untrusted_generated", summary_source: SUMMARY_SOURCE_GENERATED,
    });
    expect(view.trust).toBe("untrusted_generated");
    expect(view.generated).toBe(true);
    expect(view.caution).toBe(true);
    expect(view.label).toContain("not a HarkenIQ recommendation");
  });

  it("cautions on device-reported text", () => {
    const view = summarySourceView({ summary_trust: "untrusted_telemetry" });
    expect(view.caution).toBe(true);
    expect(view.label).toBe(LABELS.untrusted_telemetry);
  });

  it("gives every class its own words", () => {
    const labels = TRUST_CLASSES.map((t) => summarySourceView({ summary_trust: t }).label);
    expect(new Set(labels).size).toBe(TRUST_CLASSES.length);
  });
});

describe("anything the server did not state is unverified, never HarkenIQ's", () => {
  it.each([
    [undefined],
    [null],
    [{}],
    [{ summary_trust: "" }],
    [{ summary_trust: "trusted" }],
    [{ summary_trust: "DETERMINISTIC" }],
  ])("%j reads as an unstated source", (rec) => {
    const view = summarySourceView(rec as never);
    expect(view.trust).toBe("unknown");
    expect(view.caution).toBe(true);
    expect(view.label).toBe(LABELS.unknown);
  });

  it("refuses a quoted suggestion that claims to be HarkenIQ's own", () => {
    for (const claimed of ["deterministic", "operator_supplied"]) {
      const view = summarySourceView({
        summary_trust: claimed, summary_source: SUMMARY_SOURCE_GENERATED,
      });
      expect(view.trust).toBe("unknown");
      expect(view.caution).toBe(true);
    }
  });
});

describe("the incident page renders the source beside the summary", () => {
  it("draws the model's words as the model's", () => {
    const html = renderToStaticMarkup(
      <SummarySourceNote
        rec={{ summary_trust: "untrusted_generated", summary_source: SUMMARY_SOURCE_GENERATED }}
      />,
    );
    expect(html).toContain('data-testid="summary-source"');
    expect(html).toContain('data-trust="untrusted_generated"');
    expect(html).toContain("not a HarkenIQ recommendation");
  });

  it("draws HarkenIQ's own step plainly", () => {
    const html = renderToStaticMarkup(
      <SummarySourceNote
        rec={{ summary_trust: "deterministic", summary_source: SUMMARY_SOURCE_PLATFORM }}
      />,
    );
    expect(html).toContain('data-trust="deterministic"');
    expect(html).toContain("Source: HarkenIQ.");
  });

  it("never echoes the summary or any other server string", () => {
    const html = renderToStaticMarkup(
      <SummarySourceNote rec={{ summary_trust: MODEL_TEXT, summary_source: MODEL_TEXT }} />,
    );
    expect(html).not.toContain(MODEL_TEXT);
    expect(html).toContain('data-trust="unknown"');
  });
});
