/** A34.5 + A34.9: the view-model layer, and the authority it must not have.
 *
 *  A28.23: "PX is uniquely able to create a second governance universe by
 *  accident, because a UI that computes an answer IS a second authority."
 *  These are the structural guards PX1A adds for its own layer:
 *
 *  1. A view-model is a pure function of what the server sent. It imports
 *     neither React nor the API client nor the session, so it cannot fetch,
 *     cannot render, and cannot consult who is asking.
 *  2. The operational context's scope facts are a SELF-DESCRIPTION, never a
 *     read predictor (`/scope-grants/me` publishes a permission-neutral
 *     site set). Only the shell may import that module, so no page can use
 *     it to decide what to show.
 *  3. PX1A's new modules consult no permission. Each page asks Central
 *     Command; the shared foundation never pre-decides on its behalf.
 */

import ts from "typescript";
import { describe, expect, it } from "vitest";

const VIEW_MODELS = import.meta.glob<string>(["./*.ts", "!./*.test.ts"], {
  query: "?raw",
  import: "default",
  eager: true,
});

const ALL_SOURCES = import.meta.glob<string>(
  ["../**/*.{ts,tsx}", "!../**/*.test.{ts,tsx}", "!../test/**"],
  { query: "?raw", import: "default", eager: true },
);

/** PX1A's new shared modules (A34). */
const FOUNDATION = [
  "../routes.ts",
  "../useUrlState.ts",
  "../useDataState.ts",
  "../apiErrors.ts",
  "../terminology.ts",
  "../a11y/overlay.ts",
  "../a11y/useRouteFocus.ts",
  "../context/OperationalContext.tsx",
  "../components/DataState.tsx",
  "./vocabulary.ts",
  "./dataState.ts",
  "./operationalContext.ts",
];

function importsOf(source: string, file: string): string[] {
  const sf = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true);
  return sf.statements
    .filter(ts.isImportDeclaration)
    .map((d) => (d.moduleSpecifier as ts.StringLiteral).text);
}

describe("a view-model is pure", () => {
  it("there are view-models to check (non-vacuity)", () => {
    expect(Object.keys(VIEW_MODELS).length).toBeGreaterThanOrEqual(8);
  });

  it.each(Object.entries(VIEW_MODELS))("%s imports no React, no API client, no session", (file, source) => {
    const forbidden = importsOf(source, file).filter(
      (m) =>
        m === "react" ||
        m.startsWith("react-") ||
        m === "../api" ||
        m === "../useAuth" ||
        m === "../permissions",
    );
    expect(forbidden).toEqual([]);
  });
});

describe("the context's scope is a self-description, not a read predictor", () => {
  const SHELL = new Set(["../context/OperationalContext.tsx", "../components/TenantContextHeader.tsx"]);

  it("only the shell imports the operational context model", () => {
    const importers = Object.entries(ALL_SOURCES)
      .filter(([file, source]) =>
        importsOf(source, file).some((m) => m.endsWith("viewModel/operationalContext")),
      )
      .map(([file]) => file);
    expect(importers.length).toBeGreaterThan(0);
    expect(importers.filter((f) => !SHELL.has(f))).toEqual([]);
  });
});

describe("the foundation consults no permission", () => {
  it.each(FOUNDATION)("%s", (file) => {
    const source = file.startsWith("./") ? VIEW_MODELS[file] : ALL_SOURCES[file];
    expect(source, `${file} is listed but not found`).toBeTypeOf("string");
    expect(importsOf(source, file).filter((m) => m.endsWith("/permissions"))).toEqual([]);
    expect(source).not.toMatch(/\bcanAccess\(|\bcan\(user/);
  });
});
