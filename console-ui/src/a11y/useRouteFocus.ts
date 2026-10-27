import { type RefObject, useEffect, useRef } from "react";
import { useLocation } from "react-router-dom";
import { documentTitle, pageForPath } from "../routes";

/** A34.8: arriving on a page is announced, by title and by focus.
 *
 *  A single-page app changes its content without the browser noticing, so
 *  nothing tells a screen-reader user the page changed and focus stays on
 *  a sidebar link that may no longer mean anything. Two corrections:
 *
 *  - `document.title` names the page (it was "HarkenIQ Console" everywhere);
 *  - when the PAGE changes, focus moves to the page's heading (or the main
 *    region if it has none), which a screen reader reads aloud.
 *
 *  Opening or closing an object (an incident drawer) is not a page change,
 *  so it does not move focus: the drawer manages its own (`useOverlay`).
 *  The first render does not move focus either -- that would skip the skip
 *  link on a page someone has just opened.
 */
export function useRouteFocus(mainRef: RefObject<HTMLElement | null>): void {
  const { pathname } = useLocation();
  const page = pageForPath(pathname);
  const pageKey = page ? `${page.plane}:${page.path}` : pathname;
  const previous = useRef<string | null>(null);

  useEffect(() => {
    document.title = documentTitle(page);
  }, [page]);

  useEffect(() => {
    const first = previous.current === null;
    const changed = previous.current !== pageKey;
    previous.current = pageKey;
    if (first || !changed) return;
    const main = mainRef.current;
    if (!main) return;
    const heading = main.querySelector<HTMLElement>("h1");
    const target = heading ?? main;
    if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
    target.setAttribute("data-focus-target", "");
    target.focus();
  }, [pageKey, mainRef]);
}
