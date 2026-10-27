/** A35.7: the contrast contract.
 *
 *  axe running in jsdom cannot measure contrast (A34), so this test does
 *  the arithmetic itself: it reads `styles/tokens.css`, resolves each
 *  semantic token down the `var()` chain to a raw colour, composites any
 *  transparency over what it sits on, and computes the WCAG ratio for every
 *  pair the Console draws. Text needs 4.5:1; control boundaries and focus
 *  indicators need 3:1 (WCAG 1.4.3, 1.4.11). A palette change that breaks a
 *  pair fails CI before anyone sees it.
 */

import { describe, expect, it } from "vitest";
import tokensCss from "../styles/tokens.css?raw";
import { TONES, toneFamily, type Tone } from "../viewModel/status";

type Rgba = [number, number, number, number];

function declarations(css: string): Map<string, string> {
  const map = new Map<string, string>();
  const noComments = css.replace(/\/\*[\s\S]*?\*\//g, "");
  // Only the first :root block: the reduced-motion block redefines motion,
  // never a colour.
  const root = noComments.slice(noComments.indexOf(":root"), noComments.indexOf("@media"));
  for (const m of root.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) map.set(m[1], m[2].trim());
  return map;
}

const TOKENS = declarations(tokensCss);

function resolve(value: string, seen: string[] = []): string {
  const m = value.match(/^var\(\s*(--[\w-]+)\s*\)$/);
  if (!m) return value;
  if (seen.includes(m[1])) throw new Error(`cycle through ${m[1]}`);
  const next = TOKENS.get(m[1]);
  if (next === undefined) throw new Error(`undefined token ${m[1]}`);
  return resolve(next, [...seen, m[1]]);
}

function parse(colour: string): Rgba {
  const c = colour.trim().toLowerCase();
  const hex = c.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/);
  if (hex) {
    const h = hex[1].length === 3 ? [...hex[1]].map((x) => x + x).join("") : hex[1];
    return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)).concat(1) as Rgba;
  }
  const rgb = c.match(/^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)$/);
  if (rgb) return [Number(rgb[1]), Number(rgb[2]), Number(rgb[3]), rgb[4] === undefined ? 1 : Number(rgb[4])];
  throw new Error(`not a colour: ${colour}`);
}

/** A colour as drawn: transparent layers composited, top first, over an
 *  opaque base. */
function paint(layers: string[]): Rgba {
  const stack = layers.map((t) => parse(resolve(`var(${t})`)));
  let [r, g, b] = stack[stack.length - 1];
  for (let i = stack.length - 2; i >= 0; i -= 1) {
    const [fr, fg, fb, fa] = stack[i];
    [r, g, b] = [fr * fa + r * (1 - fa), fg * fa + g * (1 - fa), fb * fa + b * (1 - fa)];
  }
  return [r, g, b, 1];
}

function luminance([r, g, b]: Rgba): number {
  const lin = (v: number) => {
    const s = v / 255;
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

function ratio(fg: Rgba, bg: Rgba): number {
  const [hi, lo] = [luminance(fg), luminance(bg)].sort((a, b) => b - a);
  return (hi + 0.05) / (lo + 0.05);
}

/** fg drawn over bg; bg may be a stack (an rgba surface over its base). */
function contrast(fg: string, bg: string[]): number {
  const base = paint(bg);
  const top = parse(resolve(`var(${fg})`));
  const drawn: Rgba = top[3] < 1
    ? [top[0] * top[3] + base[0] * (1 - top[3]), top[1] * top[3] + base[1] * (1 - top[3]), top[2] * top[3] + base[2] * (1 - top[3]), 1]
    : top;
  return ratio(drawn, base);
}

const TEXT = 4.5;
const UI = 3;

const LIGHT_SURFACES = [
  ["--surface-raised"],
  ["--surface-canvas"],
  ["--surface-sunken"],
  ["--surface-hover"],
  ["--surface-selected"],
];

const INVERSE = ["--surface-inverse"];
const INVERSE_SELECTED = ["--surface-inverse-selected", "--surface-inverse"];
const INVERSE_HOVER = ["--surface-inverse-hover", "--surface-inverse"];

function family(tone: Tone): string {
  return toneFamily(tone) === "severity" ? `--severity-${tone}` : `--state-${tone}`;
}

const PAIRS: [string, string[], number, string][] = [
  ...["--text-primary", "--text-secondary", "--text-muted"].flatMap((fg) =>
    LIGHT_SURFACES.map((bg) => [fg, bg, TEXT, "text on a light surface"] as [string, string[], number, string]),
  ),
  ...["--surface-raised", "--surface-canvas", "--surface-selected"].map(
    (bg) => ["--text-link", [bg], TEXT, "a link"] as [string, string[], number, string],
  ),
  ["--text-link-hover", ["--surface-raised"], TEXT, "a hovered link"],
  ...["--action-primary-bg", "--action-primary-bg-hover", "--action-primary-bg-active"].map(
    (bg) => ["--action-primary-fg", [bg], TEXT, "a primary button"] as [string, string[], number, string],
  ),
  ...["--action-danger-bg", "--action-danger-bg-hover"].map(
    (bg) => ["--action-danger-fg", [bg], TEXT, "a danger button"] as [string, string[], number, string],
  ),
  ...["--action-secondary-bg", "--action-secondary-bg-hover", "--action-secondary-bg-active"].map(
    (bg) => ["--action-secondary-fg", [bg], TEXT, "a secondary button"] as [string, string[], number, string],
  ),
  ["--action-secondary-fg", ["--action-ghost-bg-hover", "--surface-raised"], TEXT, "a ghost button, hovered"],
  ["--action-danger-bg", ["--action-secondary-bg"], TEXT, "an outlined danger action in a table"],
  // Toasts: the message on its own fill.
  ...["--severity-healthy-fg", "--severity-critical-fg", "--surface-inverse"].map(
    (bg) => ["--text-on-emphasis", [bg], TEXT, "a toast"] as [string, string[], number, string],
  ),
  // Dark chrome.
  ["--text-inverse", INVERSE, TEXT, "sidebar text"],
  ["--text-inverse", INVERSE_SELECTED, TEXT, "the current sidebar item"],
  ["--text-inverse", INVERSE_HOVER, TEXT, "a hovered sidebar item"],
  ["--text-inverse-secondary", INVERSE, TEXT, "sidebar items"],
  ["--text-inverse-muted", INVERSE, TEXT, "sidebar section labels"],
  // Every tone: its words on its own fill, and on a plain row.
  ...TONES.flatMap((tone) => [
    [`${family(tone)}-fg`, [`${family(tone)}-bg`], TEXT, `${tone} badge`] as [string, string[], number, string],
    [`${family(tone)}-fg`, ["--surface-raised"], TEXT, `${tone} words on a row`] as [string, string[], number, string],
    ["--text-primary", [`${family(tone)}-bg`], TEXT, `text in a ${tone} banner`] as [string, string[], number, string],
  ]),
  // Boundaries and focus (WCAG 1.4.11).
  ["--border-control", ["--surface-raised"], UI, "a control's boundary"],
  ["--border-control", ["--surface-canvas"], UI, "a control's boundary on the page"],
  ["--severity-critical-fg", ["--surface-raised"], UI, "an invalid control's boundary"],
  ...LIGHT_SURFACES.map((bg) => ["--focus-ring-color", bg, UI, "the focus ring"] as [string, string[], number, string]),
  ["--focus-ring-color-inverse", INVERSE, UI, "the focus ring on dark chrome"],
  ["--focus-ring-color-inverse", INVERSE_SELECTED, UI, "the focus ring on the current sidebar item"],
  ["--text-on-emphasis", ["--severity-critical-fg"], UI, "the focus ring inside an error toast"],
  ["--action-primary-bg", ["--surface-raised"], UI, "a checked checkbox, a selected row's border"],
];

describe("the contrast contract (A35.7)", () => {
  it("every semantic colour resolves to a raw colour in the reference palette", () => {
    const COLOUR_ROLE =
      /^--((surface|text|action|brand|severity|state)-|border-(subtle|default|control|strong|inverse)$|focus-ring-color)/;
    const colourRoles = [...TOKENS.keys()].filter((name) => COLOUR_ROLE.test(name));
    expect(colourRoles.length).toBeGreaterThan(60);
    for (const name of colourRoles) {
      const value = TOKENS.get(name)!;
      expect(value, `${name} names a reference colour`).toMatch(/^var\(--ref-[\w-]+\)$/);
      expect(() => parse(resolve(value)), name).not.toThrow();
    }
  });

  it.each(PAIRS)("%s on %j is at least %s:1 (%s)", (fg, bg, min) => {
    expect(contrast(fg, bg)).toBeGreaterThanOrEqual(min);
  });

  it("would have caught the checkpoint's failures (non-vacuity)", () => {
    const old = (fg: string, bg: string) => ratio(parse(fg), parse(bg));
    expect(old("#9aa2b1", "#ffffff")).toBeLessThan(TEXT); // --text-muted, 2.57
    expect(old("#ef6c00", "#fff3e0")).toBeLessThan(TEXT); // warning badge, 2.81
    expect(old("#d8dce3", "#ffffff")).toBeLessThan(UI); // input border, 1.38
    expect(old("#632ca6", "#1c2330")).toBeLessThan(UI); // focus ring in the sidebar, 1.83
  });

  it("D1: the accent is teal #0E7A73, and #632ca6 is gone from the palette", () => {
    expect(resolve("var(--action-primary-bg)").toLowerCase()).toBe("#0e7a73");
    expect(tokensCss.toLowerCase()).not.toContain("#632ca6");
  });
});
