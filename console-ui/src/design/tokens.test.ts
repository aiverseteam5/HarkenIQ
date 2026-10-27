/** A35.3: the typed facade names only tokens the stylesheet defines, and
 *  only semantic ones. */

import { describe, expect, it } from "vitest";
import tokensCss from "../styles/tokens.css?raw";
import * as tokens from "./tokens";
import { TONES } from "../viewModel/status";

const DEFINED = new Set([...tokensCss.matchAll(/(--[\w-]+)\s*:/g)].map((m) => m[1]));

function namesIn(value: unknown, out: string[] = []): string[] {
  if (typeof value === "string") {
    for (const m of value.matchAll(/var\((--[\w-]+)\)/g)) out.push(m[1]);
  } else if (value && typeof value === "object") {
    for (const v of Object.values(value)) namesIn(v, out);
  }
  return out;
}

const FACADE = namesIn(Object.fromEntries(Object.entries(tokens).filter(([, v]) => typeof v !== "function")));
const TONE_COLOURS = TONES.flatMap((t) =>
  (["fg", "bg", "border"] as const).flatMap((part) => namesIn(tokens.toneColor(t, part))),
);

describe("the typed token facade (A35.3)", () => {
  it("names tokens (non-vacuity)", () => {
    expect(FACADE.length).toBeGreaterThan(60);
    expect(TONE_COLOURS).toHaveLength(36);
  });

  it.each([...new Set([...FACADE, ...TONE_COLOURS])])("%s is defined in tokens.css", (name) => {
    expect(DEFINED.has(name)).toBe(true);
  });

  it("never names the reference palette: components name roles, not raw colours", () => {
    expect([...FACADE, ...TONE_COLOURS].filter((n) => n.startsWith("--ref-"))).toEqual([]);
  });
});
