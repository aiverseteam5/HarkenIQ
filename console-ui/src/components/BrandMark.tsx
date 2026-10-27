import { TERMS } from "../terminology";

/** A35.8: HarkenIQ's mark and lockup.
 *
 *  One mark concept, the hexagon, drawn as owned SVG rather than a font
 *  glyph, so it renders the same everywhere. Constraints: one colour, at
 *  least 16px, clear space of half its height, and never used as a UI icon.
 *  Final artwork is PX8's; this is the foundation it will replace.
 */

export function BrandMark({ size = 20 }: { size?: number }) {
  const s = Math.max(16, size);
  return (
    <svg
      className="brand-lockup__mark"
      viewBox="0 0 16 16"
      width={s}
      height={s}
      aria-hidden="true"
      focusable="false"
      data-brand-mark=""
    >
      <path d="M8 .8l6.24 3.6v7.2L8 15.2l-6.24-3.6V4.4z" fill="currentColor" />
    </svg>
  );
}

/** Mark, wordmark and plane name: "HarkenIQ Console" (D4, A35.2). The words
 *  are the accessible name; the mark is decoration. */
export function BrandLockup({ inverse = false, size = 20 }: { inverse?: boolean; size?: number }) {
  return (
    <span className={inverse ? "brand-lockup brand-lockup--inverse" : "brand-lockup"}>
      <BrandMark size={size} />
      <span className="brand-lockup__wordmark">{TERMS.product}</span>
      <span className="brand-lockup__plane">{TERMS.console}</span>
    </span>
  );
}
