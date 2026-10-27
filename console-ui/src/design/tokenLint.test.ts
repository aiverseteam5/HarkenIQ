/** A35.9: the token lint.
 *
 *  A structural test in the house style of `terminology.test.ts`: it reads
 *  every Console source through the TypeScript AST and every stylesheet as
 *  text, so it runs in `ui-build` with no ESLint or Stylelint.
 *
 *  ZERO TOLERANCE -- each of these was a real defect the checkpoint found:
 *   1. every `var(--x)` names a defined token (13 names were not);
 *   2. every static class name is defined (`btn-sm` was not, 19 times);
 *   3. raw colour lives only in the reference palette;
 *   4. shared components, layouts and design files use tokens for spacing,
 *      type, radius, shadow, z-index and motion;
 *   5. every StatusBadge tone comes from `statusTone`, and no page-local
 *      status table survives (there were 42);
 *   6. `outline: none` appears only beside a replacement;
 *   7. no text below 11px.
 *
 *  RATCHET -- legacy measured, and allowed only to shrink:
 *   page style literals, raw colour in pages, deprecated-alias references
 *   and byte-identical style constants, per `token-baseline.json`. A file
 *   that is not in the baseline starts at zero. When a count falls, the
 *   baseline must be lowered with it, so the ratchet never goes slack.
 */

import ts from "typescript";
import { describe, expect, it } from "vitest";
import baseline from "./token-baseline.json";

/* ── Sources ──────────────────────────────────────── */

const CSS_FILES = import.meta.glob<string>("../styles/*.css", { query: "?raw", import: "default", eager: true });
const TS_FILES = import.meta.glob<string>(
  ["../**/*.{ts,tsx}", "!../**/*.test.{ts,tsx}", "!../test/**", "!../vite-env.d.ts"],
  { query: "?raw", import: "default", eager: true },
);

const rel = (key: string) => key.replace(/^(\.\.\/)+/, "").replace(/^\.\//, "design/");

const SOURCES = Object.fromEntries(Object.entries(TS_FILES).map(([k, v]) => [rel(k), v]));
const STYLES = Object.fromEntries(Object.entries(CSS_FILES).map(([k, v]) => [rel(k), v]));

/** Shared code is held to zero; pages and the app shell file are ratcheted. */
const SHARED = /^(components|layouts|design)\//;

/* ── CSS ──────────────────────────────────────────── */

interface CssRule {
  file: string;
  selector: string;
  media: string;
  decls: [string, string][];
}

function cssRules(file: string, css: string): CssRule[] {
  const text = css.replace(/\/\*[\s\S]*?\*\//g, "").replace(/@import[^;]+;/g, "");
  const out: CssRule[] = [];
  const walk = (src: string, media: string) => {
    let i = 0;
    while (i < src.length) {
      const open = src.indexOf("{", i);
      if (open < 0) break;
      const prelude = src.slice(i, open).trim();
      let depth = 1;
      let j = open + 1;
      while (depth && j < src.length) {
        if (src[j] === "{") depth += 1;
        else if (src[j] === "}") depth -= 1;
        j += 1;
      }
      const body = src.slice(open + 1, j - 1);
      if (prelude.startsWith("@media")) walk(body, prelude);
      else if (!prelude.startsWith("@keyframes")) {
        const decls = body
          .split(";")
          .map((d) => d.trim())
          .filter((d) => d.includes(":"))
          .map((d) => [d.slice(0, d.indexOf(":")).trim(), d.slice(d.indexOf(":") + 1).trim()] as [string, string]);
        out.push({ file, selector: prelude, media, decls });
      }
      i = j;
    }
  };
  walk(text, "");
  return out;
}

const RULES = Object.entries(STYLES).flatMap(([f, css]) => cssRules(f, css));

const DEFINED_TOKENS = new Set(RULES.flatMap((r) => r.decls.filter(([p]) => p.startsWith("--")).map(([p]) => p)));
const DEFINED_CLASSES = new Set(RULES.flatMap((r) => [...r.selector.matchAll(/\.([a-zA-Z][\w-]*)/g)].map((m) => m[1])));
const DEPRECATED = new Set(
  RULES.filter((r) => r.file === "styles/variables.css").flatMap((r) => r.decls.map(([p]) => p)),
);

const COLOUR = /(?<![\w&])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})(?![\w-])|\b(?:rgba?|hsla?)\(/g;
const colours = (text: string) => [...text.matchAll(COLOUR)].map((m) => m[0]);

/* Properties whose literal value bypasses a token (A35.4). */
const TRACKED_CSS =
  /^(padding|padding-(top|right|bottom|left|block|inline)|margin|margin-(top|right|bottom|left|block|inline)|gap|row-gap|column-gap|font-size|line-height|font-weight|letter-spacing|border-radius|box-shadow|z-index|transition|transition-duration|animation|animation-duration)$/;
const KEYWORDS = new Set(["0", "auto", "none", "inherit", "normal", "initial", "unset", "transparent"]);
const isTokenized = (value: string) =>
  value.includes("var(") || value.split(/\s+/).every((part) => KEYWORDS.has(part.replace(/!important/, "")));

/** The clip-path pattern of `.sr-only`, and the reduced-motion override,
 *  are standard and deliberate literals. */
const CSS_LITERAL_ALLOWED = (r: CssRule) => r.selector === ".sr-only" || r.media.includes("prefers-reduced-motion");

/* ── TypeScript ───────────────────────────────────── */

const TRACKED_TS = new Set([
  "padding", "paddingTop", "paddingRight", "paddingBottom", "paddingLeft", "paddingBlock", "paddingInline",
  "margin", "marginTop", "marginRight", "marginBottom", "marginLeft", "marginBlock", "marginInline",
  "gap", "rowGap", "columnGap", "fontSize", "lineHeight", "fontWeight", "letterSpacing",
  "borderRadius", "boxShadow", "zIndex", "transition", "animation",
]);

interface Facts {
  styleLiterals: string[];
  rawColours: string[];
  varRefs: string[];
  varDefs: string[];
  classTokens: string[];
  badStatusBadges: string[];
  badgeCount: number;
  statusTables: string[];
  outlineNone: string[];
  smallFonts: string[];
  styleConstants: string[];
}

function propName(name: ts.PropertyName): string {
  return ts.isIdentifier(name) || ts.isStringLiteral(name) || ts.isNoSubstitutionTemplateLiteral(name) ? name.text : "";
}

/** The literal leaves of a style value, through conditionals and fallbacks. */
function literalLeaves(e: ts.Expression): string[] {
  if (ts.isParenthesizedExpression(e)) return literalLeaves(e.expression);
  if (ts.isConditionalExpression(e)) return [...literalLeaves(e.whenTrue), ...literalLeaves(e.whenFalse)];
  if (ts.isBinaryExpression(e) && [ts.SyntaxKind.BarBarToken, ts.SyntaxKind.QuestionQuestionToken].includes(e.operatorToken.kind))
    return [...literalLeaves(e.left), ...literalLeaves(e.right)];
  if (ts.isStringLiteral(e) || ts.isNoSubstitutionTemplateLiteral(e)) return isTokenized(e.text) ? [] : [e.text];
  if (ts.isNumericLiteral(e)) return Number(e.text) === 0 ? [] : [e.text];
  if (ts.isTemplateExpression(e)) return e.getText().includes("var(") ? [] : [e.getText()];
  return [];
}

function pxOf(value: string): number | null {
  const m = value.trim().match(/^([\d.]+)(rem|px)$/);
  if (!m) return null;
  return m[2] === "rem" ? Number(m[1]) * 16 : Number(m[1]);
}

function classTokensIn(e: ts.Node, out: string[]) {
  if (ts.isStringLiteral(e) || ts.isNoSubstitutionTemplateLiteral(e)) out.push(...e.text.split(/\s+/));
  else if (ts.isTemplateExpression(e)) {
    // A token touching a substitution is partial (`toast--${variant}`).
    const parts = [e.head.text, ...e.templateSpans.map((s) => s.literal.text)];
    parts.forEach((part, i) => {
      const words = part.split(/\s+/);
      words.forEach((w, k) => {
        const touchesLeft = i > 0 && k === 0;
        const touchesRight = i < parts.length - 1 && k === words.length - 1;
        if (!touchesLeft && !touchesRight) out.push(w);
      });
    });
  } else if (ts.isConditionalExpression(e)) {
    // The condition decides WHICH class; only the branches ARE classes.
    classTokensIn(e.whenTrue, out);
    classTokensIn(e.whenFalse, out);
  } else if (ts.isBinaryExpression(e)) {
    const k = e.operatorToken.kind;
    if ([ts.SyntaxKind.PlusToken, ts.SyntaxKind.BarBarToken, ts.SyntaxKind.QuestionQuestionToken].includes(k)) {
      classTokensIn(e.left, out);
      classTokensIn(e.right, out);
    }
    // A comparison's operands are values, not classes.
  } else if (!ts.isElementAccessExpression(e)) {
    // A keyed lookup (`ACTION_CLASS[variant]`) names a key, not a class.
    ts.forEachChild(e, (c) => classTokensIn(c, out));
  }
}

function factsOf(file: string, source: string): Facts {
  const f: Facts = {
    styleLiterals: [], rawColours: [], varRefs: [], varDefs: [], classTokens: [],
    badStatusBadges: [], badgeCount: 0, statusTables: [], outlineNone: [], smallFonts: [], styleConstants: [],
  };
  const sf = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const at = (n: ts.Node) => `${file}:${sf.getLineAndCharacterOfPosition(n.getStart()).line + 1}`;
  const visit = (n: ts.Node) => {
    if (ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n) || ts.isTemplateExpression(n)) {
      const text = ts.isTemplateExpression(n) ? n.getText() : n.text;
      f.rawColours.push(...colours(text).map((c) => `${at(n)} ${c}`));
      for (const m of text.matchAll(/var\(\s*(--[\w-]+)/g)) f.varRefs.push(m[1]);
    }
    if (ts.isPropertyAssignment(n)) {
      const name = propName(n.name);
      if (name.startsWith("--")) f.varDefs.push(name);
      if (TRACKED_TS.has(name)) f.styleLiterals.push(...literalLeaves(n.initializer).map((v) => `${at(n)} ${name}: ${v}`));
      if (name === "outline" && literalLeaves(n.initializer).some((v) => v === "none")) f.outlineNone.push(at(n));
      if (name === "outline" && ts.isStringLiteral(n.initializer) && n.initializer.text === "none") f.outlineNone.push(at(n));
      if (name === "fontSize") {
        const leaves = [n.initializer].flatMap((e) =>
          ts.isStringLiteral(e) || ts.isNoSubstitutionTemplateLiteral(e) ? [e.text] : ts.isNumericLiteral(e) ? [`${e.text}px`] : [],
        );
        for (const v of leaves) {
          const px = pxOf(v);
          if (px !== null && px < 11) f.smallFonts.push(`${at(n)} ${v}`);
        }
      }
    }
    if (ts.isJsxAttribute(n) && ts.isIdentifier(n.name) && n.name.text === "className" && n.initializer) {
      classTokensIn(n.initializer, f.classTokens);
    }
    if ((ts.isJsxSelfClosingElement(n) || ts.isJsxOpeningElement(n)) && n.tagName.getText() === "StatusBadge") {
      f.badgeCount += 1;
      const attrs = n.attributes.properties.filter(ts.isJsxAttribute);
      const tone = attrs.find((a) => ts.isIdentifier(a.name) && a.name.text === "tone");
      const viaModule =
        tone?.initializer &&
        ts.isJsxExpression(tone.initializer) &&
        tone.initializer.expression &&
        ts.isCallExpression(tone.initializer.expression) &&
        tone.initializer.expression.expression.getText() === "statusTone";
      if (!viaModule) f.badStatusBadges.push(at(n));
    }
    if (ts.isVariableDeclaration(n) && ts.isIdentifier(n.name)) {
      if (/(VARIANT|COLOR|COLOUR|TONE)S?$/.test(n.name.text) && n.initializer && ts.isObjectLiteralExpression(n.initializer))
        f.statusTables.push(`${at(n)} ${n.name.text}`);
      if (n.type?.getText() === "CSSProperties" && n.initializer && ts.isObjectLiteralExpression(n.initializer))
        f.styleConstants.push(n.initializer.getText().replace(/\s+/g, ""));
    }
    ts.forEachChild(n, visit);
  };
  visit(sf);
  return f;
}

const FACTS = Object.fromEntries(Object.entries(SOURCES).map(([file, src]) => [file, factsOf(file, src)]));
const ALL_FACTS = Object.values(FACTS);
const DEFINED = new Set([...DEFINED_TOKENS, ...ALL_FACTS.flatMap((f) => f.varDefs)]);

/* ── Ratchet ──────────────────────────────────────── */

const counts = (pick: (f: Facts) => number, files = Object.keys(FACTS)) =>
  Object.fromEntries(files.map((file) => [file, pick(FACTS[file])]).filter(([, n]) => (n as number) > 0)) as Record<string, number>;

const duplicateConstants = (() => {
  const seen = new Map<string, number>();
  for (const body of ALL_FACTS.flatMap((f) => f.styleConstants)) seen.set(body, (seen.get(body) ?? 0) + 1);
  return [...seen.values()].filter((n) => n > 1).reduce((a, b) => a + b, 0);
})();

const deprecatedRefs = (): Record<string, number> => {
  const out: Record<string, number> = {};
  for (const [file, f] of Object.entries(FACTS)) {
    const n = f.varRefs.filter((v) => DEPRECATED.has(v)).length;
    if (n) out[file] = n;
  }
  for (const [file, css] of Object.entries(STYLES)) {
    if (file === "styles/variables.css") continue;
    const n = [...css.matchAll(/var\(\s*(--[\w-]+)/g)].filter((m) => DEPRECATED.has(m[1])).length;
    if (n) out[file] = n;
  }
  return out;
};

const CURRENT = {
  styleLiterals: counts((f) => f.styleLiterals.length, Object.keys(FACTS).filter((f) => !SHARED.test(f))),
  rawColours: counts((f) => f.rawColours.length, Object.keys(FACTS).filter((f) => f.startsWith("pages/"))),
  deprecatedAliases: deprecatedRefs(),
  duplicateConstants,
};

type Ledger = Record<string, number>;
const BASE = baseline as { styleLiterals: Ledger; rawColours: Ledger; deprecatedAliases: Ledger; duplicateConstants: number };

function overAndSlack(now: Ledger, was: Ledger) {
  const over = Object.entries(now).filter(([f, n]) => n > (was[f] ?? 0)).map(([f, n]) => `${f}: ${n} > ${was[f] ?? 0}`);
  const slack = Object.entries(was).filter(([f, n]) => (now[f] ?? 0) < n).map(([f, n]) => `${f}: ${now[f] ?? 0} < ${n}`);
  return { over, slack };
}

/* ── Tests ────────────────────────────────────────── */

describe("the token lint reads the whole Console (non-vacuity)", () => {
  it("sees sources, stylesheets, tokens and classes", () => {
    expect(Object.keys(SOURCES).length).toBeGreaterThan(60);
    expect(Object.keys(STYLES).sort()).toEqual(["styles/global.css", "styles/tokens.css", "styles/variables.css"]);
    expect(DEFINED_TOKENS.size).toBeGreaterThan(150);
    expect(DEFINED_CLASSES.has("btn-sm")).toBe(true);
    expect(ALL_FACTS.reduce((n, f) => n + f.badgeCount, 0)).toBeGreaterThanOrEqual(106);
  });
});

describe("zero tolerance (A35.9)", () => {
  it("1. every var(--x) names a defined token", () => {
    const refs = [
      ...ALL_FACTS.flatMap((f) => f.varRefs),
      ...Object.values(STYLES).flatMap((css) => [...css.matchAll(/var\(\s*(--[\w-]+)/g)].map((m) => m[1])),
    ];
    expect(refs.length).toBeGreaterThan(500);
    expect([...new Set(refs.filter((r) => !DEFINED.has(r)))]).toEqual([]);
  });

  it("2. every static class name is defined", () => {
    const used = ALL_FACTS.flatMap((f) => f.classTokens).filter((t) => /^[a-zA-Z][\w-]*$/.test(t));
    expect(used.length).toBeGreaterThan(150);
    expect([...new Set(used.filter((t) => !DEFINED_CLASSES.has(t)))]).toEqual([]);
  });

  it("3a. raw colour lives only in the reference palette of tokens.css", () => {
    const misplaced = RULES.flatMap((r) =>
      r.decls
        .filter(([p, v]) => colours(v).length > 0 && !(r.file === "styles/tokens.css" && p.startsWith("--ref-")))
        .map(([p, v]) => `${r.file} ${r.selector} ${p}: ${v}`),
    );
    expect(misplaced).toEqual([]);
  });

  it("3b. no raw colour in any source outside pages (pages are ratcheted)", () => {
    expect(
      Object.entries(FACTS)
        .filter(([file]) => !file.startsWith("pages/"))
        .flatMap(([, f]) => f.rawColours),
    ).toEqual([]);
  });

  it("4a. shared components, layouts and design files use tokens, not literals", () => {
    expect(
      Object.entries(FACTS)
        .filter(([file]) => SHARED.test(file))
        .flatMap(([, f]) => f.styleLiterals),
    ).toEqual([]);
  });

  it("4b. global.css uses tokens, not literals", () => {
    const literal = RULES.filter((r) => r.file === "styles/global.css" && !CSS_LITERAL_ALLOWED(r)).flatMap((r) =>
      r.decls.filter(([p, v]) => TRACKED_CSS.test(p) && !isTokenized(v)).map(([p, v]) => `${r.selector} ${p}: ${v}`),
    );
    expect(literal).toEqual([]);
  });

  it("5a. every StatusBadge tone comes from statusTone", () => {
    // The gallery's tone specimen shows each of the twelve tones by name;
    // it is a dev-only review page, never product UI (A35.11).
    const product = Object.entries(FACTS).filter(([file]) => !file.startsWith("design/gallery/"));
    expect(product.flatMap(([, f]) => f.badStatusBadges)).toEqual([]);
  });

  it("5b. no page-local status table survives", () => {
    expect(ALL_FACTS.flatMap((f) => f.statusTables)).toEqual([]);
  });

  it("6. outline: none appears only beside a replacement", () => {
    expect(ALL_FACTS.flatMap((f) => f.outlineNone)).toEqual([]);
    const bare = RULES.filter(
      (r) =>
        r.decls.some(([p, v]) => p === "outline" && /^(none|0)$/.test(v)) &&
        r.selector !== "[data-focus-target]:focus" &&
        !r.decls.some(([p]) => p === "box-shadow" || p === "border-color"),
    ).map((r) => `${r.file} ${r.selector}`);
    expect(bare).toEqual([]);
  });

  it("7. no text below 11px", () => {
    expect(ALL_FACTS.flatMap((f) => f.smallFonts)).toEqual([]);
    const small = RULES.flatMap((r) =>
      r.decls
        .filter(([p, v]) => (p === "font-size" || /^--type-[\w-]+-size$/.test(p)) && (pxOf(v) ?? 99) < 11)
        .map(([p, v]) => `${r.file} ${p}: ${v}`),
    );
    expect(small).toEqual([]);
  });
});

describe("the ratchet (A35.9)", () => {
  it.each(["styleLiterals", "rawColours", "deprecatedAliases"] as const)("%s never grows", (key) => {
    const { over } = overAndSlack(CURRENT[key], BASE[key]);
    expect(over, `current ledger:\n${JSON.stringify(CURRENT[key], null, 2)}`).toEqual([]);
  });

  it("byte-identical style constants never grow", () => {
    expect(CURRENT.duplicateConstants).toBeLessThanOrEqual(BASE.duplicateConstants);
  });

  it("the baseline is tight: when a count falls, the baseline falls with it", () => {
    const slack = (["styleLiterals", "rawColours", "deprecatedAliases"] as const).flatMap(
      (key) => overAndSlack(CURRENT[key], BASE[key]).slack.map((s) => `${key} ${s}`),
    );
    if (CURRENT.duplicateConstants < BASE.duplicateConstants)
      slack.push(`duplicateConstants ${CURRENT.duplicateConstants} < ${BASE.duplicateConstants}`);
    expect(slack, `lower token-baseline.json to:\n${JSON.stringify(CURRENT, null, 2)}`).toEqual([]);
  });
});
