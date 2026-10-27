import { type RefObject, useEffect, useRef } from "react";

/** A34.8: an overlay owns the keyboard while it is open.
 *
 *  A28.18's non-negotiable: focus moves into an overlay when it opens,
 *  stays there while it is open, and returns to what opened it when it
 *  closes. Before this, neither `DetailPanel` nor `ConfirmDialog` did any
 *  of it, and both listened for Escape on the whole document -- so one
 *  Escape over a confirmation opened from inside a drawer (Users -> Disable
 *  User) closed the confirmation AND the drawer behind it.
 *
 *  Overlays stack. Only the topmost handles Tab and Escape, so closing the
 *  top one hands the keyboard back to the one beneath it.
 */

const stack: HTMLElement[] = [];

const FOCUSABLE = [
  "a[href]",
  "area[href]",
  "button:not([disabled])",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "iframe",
  "[contenteditable='true']",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

/** The elements Tab can reach inside `root`, in document order. */
export function focusableWithin(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (el) => el.getAttribute("aria-hidden") !== "true" && !el.closest("[inert]"),
  );
}

export function isTopOverlay(el: HTMLElement): boolean {
  return stack.length > 0 && stack[stack.length - 1] === el;
}

export interface OverlayOptions {
  /** Called when Escape is pressed while this overlay is topmost. */
  onEscape: () => void;
  /** What receives focus on open. Default: the overlay element itself,
   *  which should carry `tabIndex={-1}` and an accessible name. */
  initialFocus?: () => HTMLElement | null;
}

export function useOverlay(
  ref: RefObject<HTMLElement | null>,
  active: boolean,
  options: OverlayOptions,
): void {
  const latest = useRef(options);
  latest.current = options;

  useEffect(() => {
    if (!active) return;
    const el = ref.current;
    if (!el) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    stack.push(el);
    (latest.current.initialFocus?.() ?? el).focus();

    const onKeyDown = (e: KeyboardEvent) => {
      if (!isTopOverlay(el)) return;
      if (e.key === "Escape") {
        e.preventDefault();
        latest.current.onEscape();
        return;
      }
      if (e.key !== "Tab") return;
      const items = focusableWithin(el);
      if (items.length === 0) {
        e.preventDefault();
        el.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const current = document.activeElement;
      const outside = !(current instanceof Node) || !el.contains(current);
      if (e.shiftKey && (outside || current === first || current === el)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (outside || current === last)) {
        e.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      const at = stack.lastIndexOf(el);
      if (at >= 0) stack.splice(at, 1);
      // Back to whatever opened it -- unless that is gone (its row was
      // re-rendered away), in which case there is nothing honest to restore.
      if (opener && opener.isConnected) opener.focus();
    };
  }, [active, ref]);
}
