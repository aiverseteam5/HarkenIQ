/** A34.7: "Agent" is never a bare noun on screen (A28.7).
 *
 *  A structural test over the Console's own source, in the house style of
 *  the backend's route-contract and consumption-census tests: it reads every
 *  user-facing string position and refuses
 *
 *  - a bare "agent" / "agents" -- say Harken Node, Operational Agent or
 *    External Agent Runtime, whichever is meant;
 *  - a retired product name (`RETIRED_TERMS`).
 *
 *  User-facing positions: JSX text; string attributes that label or describe
 *  (title, label, header, placeholder, aria-label, description, message, ...);
 *  object properties with the same names (column headers, nav items,
 *  breadcrumbs, filter definitions); and the first argument of `toast(...)`.
 *  Every string inside such an expression counts, so both branches of a
 *  conditional are read.
 *
 *  Identifiers, URLs, comments and server values are NOT user-facing and
 *  are not read: `agent_id` and `/operational-agents` stay as they are.
 */

import ts from "typescript";
import { describe, expect, it } from "vitest";
import { RETIRED_TERMS, TERMS } from "./terminology";

const SOURCES = import.meta.glob<string>(
  ["./**/*.{ts,tsx}", "!./**/*.test.{ts,tsx}", "!./test/**", "!./terminology.ts"],
  { query: "?raw", import: "default", eager: true },
);

const LABEL_NAMES = new Set([
  "title",
  "label",
  "header",
  "placeholder",
  "aria-label",
  "emptyMessage",
  "subtitle",
  "description",
  "message",
  "confirmLabel",
  "cancelLabel",
  "actionLabel",
  "alt",
  "status",
]);

/** The compound terms that legitimately contain the word. */
const ALLOWED = /\b(operational agents?|external agent runtimes?)\b/gi;
const BARE_AGENT = /\bagents?\b/i;

interface Finding {
  file: string;
  line: number;
  where: string;
  text: string;
}

/** Every string literal inside `node`, however it is composed. */
function stringsIn(node: ts.Node): string[] {
  const out: string[] = [];
  const visit = (n: ts.Node) => {
    if (ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n)) out.push(n.text);
    else if (ts.isTemplateExpression(n)) {
      out.push([n.head.text, ...n.templateSpans.map((s) => s.literal.text)].join(" "));
      n.templateSpans.forEach((s) => visit(s.expression));
      return;
    }
    // A nested call's arguments are not this label's words, except through
    // its own position (a `toast` inside an attribute is read as a toast).
    if (ts.isCallExpression(n) || ts.isArrowFunction(n) || ts.isFunctionExpression(n)) return;
    ts.forEachChild(n, visit);
  };
  visit(node);
  return out;
}

function userFacingStrings(file: string, source: string): Finding[] {
  const sf = ts.createSourceFile(
    file,
    source,
    ts.ScriptTarget.Latest,
    true,
    file.endsWith("x") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const found: Finding[] = [];
  const add = (node: ts.Node, where: string, texts: string[]) => {
    const line = sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;
    for (const text of texts) found.push({ file, line, where, text });
  };
  const visit = (n: ts.Node) => {
    if (ts.isJsxText(n)) {
      const text = n.getText(sf).replace(/\s+/g, " ").trim();
      if (text) add(n, "text", [text]);
    } else if (ts.isJsxAttribute(n) && LABEL_NAMES.has(n.name.getText(sf)) && n.initializer) {
      add(n, n.name.getText(sf), stringsIn(n.initializer));
    } else if (
      ts.isPropertyAssignment(n) &&
      LABEL_NAMES.has(n.name.getText(sf).replace(/^["']|["']$/g, ""))
    ) {
      add(n, n.name.getText(sf), stringsIn(n.initializer));
    } else if (
      ts.isCallExpression(n) &&
      n.expression.getText(sf) === "toast" &&
      n.arguments.length > 0
    ) {
      add(n, "toast", stringsIn(n.arguments[0]));
    }
    ts.forEachChild(n, visit);
  };
  visit(sf);
  return found;
}

const ALL = Object.entries(SOURCES).flatMap(([file, source]) => userFacingStrings(file, source));
const show = (f: Finding) => `${f.file}:${f.line} [${f.where}] ${f.text}`;

describe("the terminology lint reads what it claims to", () => {
  it("covers every page, component and layout", () => {
    const files = Object.keys(SOURCES);
    expect(files.some((f) => f.startsWith("./pages/"))).toBe(true);
    expect(files.some((f) => f.startsWith("./components/"))).toBe(true);
    expect(files.some((f) => f.startsWith("./layouts/"))).toBe(true);
    expect(files.some((f) => f.endsWith(".test.ts") || f.endsWith(".test.tsx"))).toBe(false);
  });

  it("finds labels in every position it names (non-vacuity)", () => {
    const probe = userFacingStrings(
      "probe.tsx",
      `const cols = [{ header: "Agent ID" }];
       const x = <Thing title="New agent" placeholder={busy ? "a" : "Search agents"}>Agent Info</Thing>;
       toast(err ? "Failed to load agents" : "ok", "error");`,
    );
    const bare = probe.filter((f) => BARE_AGENT.test(f.text.replace(ALLOWED, "")));
    expect(bare.map((f) => f.where).sort()).toEqual(["header", "placeholder", "text", "title", "toast"]);
  });

  it("does not read identifiers, URLs or comments", () => {
    const probe = userFacingStrings(
      "probe.tsx",
      `// the agent loop
       const url = "/api/t/x/operational-agents/" + agent_id;
       const x = <Link to={url}>{agent.name}</Link>;`,
    );
    expect(probe).toEqual([]);
  });
});

describe(`"Agent" is never a bare noun on screen (A28.7)`, () => {
  it("no user-facing string uses a bare agent", () => {
    const bare = ALL.filter((f) => BARE_AGENT.test(f.text.replace(ALLOWED, "")));
    expect(
      bare.map(show),
      `say ${TERMS.harkenNode}, ${TERMS.operationalAgent} or ${TERMS.externalAgentRuntime}`,
    ).toEqual([]);
  });

  it("no user-facing string uses a retired name", () => {
    const retired = ALL.filter((f) => Object.keys(RETIRED_TERMS).some((t) => f.text.includes(t)));
    expect(retired.map(show)).toEqual([]);
  });
});
