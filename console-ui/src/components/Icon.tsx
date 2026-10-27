import type { ReactNode } from "react";
import type { Tone } from "../viewModel/status";

/** A35.8: HarkenIQ's own icon family.
 *
 *  One outline style: a 16-unit grid, a 1.5 stroke, round caps and joins,
 *  drawn in `currentColor`. No emoji, no font glyphs, no third-party set.
 *  Only the glyphs a shared primitive needs today live here; navigation
 *  icons move with PX2's navigation.
 *
 *  Decorative by default (`aria-hidden`): the words beside an icon carry
 *  its meaning. Pass `label` only when the icon stands alone.
 */

const UI_GLYPHS = {
  close: <path d="M4 4l8 8M12 4l-8 8" />,
  "chevron-down": <path d="M4 6l4 4 4-4" />,
  "chevron-up": <path d="M4 10l4-4 4 4" />,
  "sort-none": <path d="M5 6l3-3 3 3M5 10l3 3 3-3" />,
  search: (
    <>
      <circle cx="7" cy="7" r="4.5" />
      <path d="M10.5 10.5L14 14" />
    </>
  ),
  "sign-out": <path d="M9.5 2.5h-6v11h6M7 8h7M11.5 5.5L14 8l-2.5 2.5" />,
} as const;

/** One distinct SHAPE per tone (A35.5 rule 7), so a tone survives
 *  greyscale and every kind of colour vision. Severity ranks by outline:
 *  octagon › diamond › triangle. */
const TONE_GLYPHS: Record<Tone, ReactNode> = {
  critical: (
    <>
      <path d="M5.3 1.5h5.4l3.8 3.8v5.4l-3.8 3.8H5.3l-3.8-3.8V5.3z" />
      <path d="M8 4.75v3.75" />
      <circle cx="8" cy="11.1" r="0.6" fill="currentColor" />
    </>
  ),
  high: (
    <>
      <path d="M8 1.5L14.5 8 8 14.5 1.5 8z" />
      <path d="M8 5v3.5" />
      <circle cx="8" cy="10.9" r="0.6" fill="currentColor" />
    </>
  ),
  warning: (
    <>
      <path d="M8 1.75l6.75 12H1.25z" />
      <path d="M8 6v3.25" />
      <circle cx="8" cy="11.6" r="0.6" fill="currentColor" />
    </>
  ),
  informational: (
    <>
      <circle cx="8" cy="8" r="6.5" />
      <path d="M8 7.5v4" />
      <circle cx="8" cy="5" r="0.6" fill="currentColor" />
    </>
  ),
  healthy: (
    <>
      <circle cx="8" cy="8" r="6.5" />
      <path d="M5.25 8.25l1.9 1.9 3.6-4" />
    </>
  ),
  neutral: <circle cx="8" cy="8" r="2.75" fill="currentColor" />,
  pending: (
    <>
      <circle cx="8" cy="8" r="6.5" />
      <path d="M8 4.75V8l2.25 1.5" />
    </>
  ),
  approved: <path d="M3 8.5l3.5 3.5L13 4.5" />,
  denied: <path d="M4 4l8 8M12 4l-8 8" />,
  blocked: (
    <>
      <circle cx="8" cy="8" r="6.5" />
      <path d="M4.75 8h6.5" />
    </>
  ),
  stale: <path d="M4.5 2h7L8 8zM4.5 14h7L8 8z" />,
  unknown: (
    <>
      <circle cx="8" cy="8" r="6.5" strokeDasharray="2 2" />
      <path d="M6.25 6.25a1.75 1.75 0 1 1 2.5 1.6c-.55.3-.75.65-.75 1.15v.5" />
      <circle cx="8" cy="11.6" r="0.6" fill="currentColor" />
    </>
  ),
};

export type UiGlyph = keyof typeof UI_GLYPHS;
export type IconName = UiGlyph | `tone-${Tone}`;

interface Props {
  name: IconName;
  /** Rendered width and height, in CSS pixels. */
  size?: number;
  /** An accessible name, for an icon that stands alone. */
  label?: string;
  className?: string;
}

export default function Icon({ name, size = 16, label, className }: Props) {
  const glyph = name.startsWith("tone-")
    ? TONE_GLYPHS[name.slice(5) as Tone]
    : UI_GLYPHS[name as UiGlyph];
  return (
    <svg
      className={className ? `icon ${className}` : "icon"}
      viewBox="0 0 16 16"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      focusable="false"
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      data-icon={name}
    >
      {glyph}
    </svg>
  );
}
