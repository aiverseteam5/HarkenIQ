/** A34.3: the harness for tests that need a DOM.
 *
 *  A test file opts in with `// @vitest-environment jsdom` on its first
 *  line; Node stays the default, so pure view-model tests stay fast.
 *  Importing this module registers cleanup after every test.
 *
 *  Three patterns, one example each in the suite:
 *  - VIEW-MODEL: a pure function over the server's closed vocabulary
 *    (`viewModel/dataState.test.ts`) -- no DOM, no harness.
 *  - COMPONENT: rendered, driven by keyboard, checked by axe
 *    (`components/overlay.test.tsx`).
 *  - ROUTE: a page reached through a URL, in a memory router
 *    (`pages/Incidents.route.test.tsx`).
 */

import { afterEach } from "vitest";
import { cleanup, render, type RenderResult } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

afterEach(() => {
  cleanup();
});

export type User = ReturnType<typeof userEvent.setup>;

/** Render `ui` with a keyboard-and-mouse user attached. */
export function renderWithUser(ui: ReactElement): RenderResult & { user: User } {
  const user = userEvent.setup();
  return { user, ...render(ui) };
}

/** The current location, rendered so a test can read where it is. */
export function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>;
}

/** Render `element` at `route`, matched by `path`, with a location probe
 *  beside it -- what a person lands on when they follow a link. */
export function renderAtRoute(
  element: ReactElement,
  { route, path }: { route: string; path: string },
): RenderResult & { user: User } {
  return renderWithUser(
    <MemoryRouter initialEntries={[route]}>
      <Routes>
        <Route path={path} element={element} />
      </Routes>
      <LocationProbe />
    </MemoryRouter>,
  );
}
