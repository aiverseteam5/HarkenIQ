/** A35.3: the semantic token tier, typed, for inline styles.
 *
 *  `styles/tokens.css` defines the tokens; this module only names them, so a
 *  misspelled token is a type error instead of an invisible border (the
 *  checkpoint found 13 names pages used and nobody defined).
 *  `tokens.test.ts` holds every name here to a definition in the stylesheet.
 *
 *  Reference-palette tokens (`--ref-*`) are deliberately absent: components
 *  and pages name roles, never raw colours (A35.3).
 */

import type { CSSProperties } from "react";
import type { Tone } from "../viewModel/status";

const v = <T extends string>(name: T) => `var(--${name})` as const;

export const surface = {
  canvas: v("surface-canvas"),
  raised: v("surface-raised"),
  sunken: v("surface-sunken"),
  hover: v("surface-hover"),
  selected: v("surface-selected"),
  scrim: v("surface-scrim"),
  inverse: v("surface-inverse"),
} as const;

export const text = {
  primary: v("text-primary"),
  secondary: v("text-secondary"),
  muted: v("text-muted"),
  disabled: v("text-disabled"),
  link: v("text-link"),
  onEmphasis: v("text-on-emphasis"),
  inverse: v("text-inverse"),
  inverseSecondary: v("text-inverse-secondary"),
  inverseMuted: v("text-inverse-muted"),
} as const;

export const border = {
  subtle: v("border-subtle"),
  default: v("border-default"),
  control: v("border-control"),
  strong: v("border-strong"),
  width: v("border-width"),
  widthStrong: v("border-width-strong"),
  widthEmphasis: v("border-width-emphasis"),
} as const;

export const action = {
  primaryBg: v("action-primary-bg"),
  primaryFg: v("action-primary-fg"),
  dangerBg: v("action-danger-bg"),
  dangerFg: v("action-danger-fg"),
} as const;

export const space = {
  "0": v("space-0"),
  "0-5": v("space-0-5"),
  "1": v("space-1"),
  "1-5": v("space-1-5"),
  "2": v("space-2"),
  "3": v("space-3"),
  "4": v("space-4"),
  "5": v("space-5"),
  "6": v("space-6"),
  "8": v("space-8"),
  "12": v("space-12"),
  "16": v("space-16"),
} as const;

export const radius = {
  sm: v("radius-sm"),
  md: v("radius-md"),
  lg: v("radius-lg"),
  pill: v("radius-pill"),
  round: v("radius-round"),
} as const;

export const elevation = {
  "1": v("elevation-1"),
  "2": v("elevation-2"),
  "3": v("elevation-3"),
} as const;

export const z = {
  sidebar: v("z-sidebar"),
  sticky: v("z-sticky"),
  drawer: v("z-drawer"),
  dialog: v("z-dialog"),
  toast: v("z-toast"),
} as const;

export const font = {
  sans: v("font-sans"),
  mono: v("font-mono"),
  weightRegular: v("font-weight-regular"),
  weightMedium: v("font-weight-medium"),
  weightSemibold: v("font-weight-semibold"),
  weightBold: v("font-weight-bold"),
} as const;

export const size = {
  controlDefault: v("size-control-default"),
  controlCompact: v("size-control-compact"),
  iconSm: v("size-icon-sm"),
  iconMd: v("size-icon-md"),
  iconLg: v("size-icon-lg"),
  measure: v("size-measure"),
} as const;

/** The twelve type roles of A35.4, as style fragments. */
export const type = {
  pageTitle: { fontSize: v("type-page-title-size"), lineHeight: v("type-page-title-line"), fontWeight: v("type-page-title-weight") },
  section: { fontSize: v("type-section-size"), lineHeight: v("type-section-line"), fontWeight: v("type-section-weight") },
  objectTitle: { fontSize: v("type-object-title-size"), lineHeight: v("type-object-title-line"), fontWeight: v("type-object-title-weight") },
  body: { fontSize: v("type-body-size"), lineHeight: v("type-body-line") },
  support: { fontSize: v("type-support-size"), lineHeight: v("type-support-line") },
  table: { fontSize: v("type-table-size"), lineHeight: v("type-table-line") },
  label: { fontSize: v("type-label-size"), lineHeight: v("type-label-line"), fontWeight: v("type-label-weight") },
  columnHeader: {
    fontSize: v("type-column-header-size"),
    lineHeight: v("type-column-header-line"),
    fontWeight: v("type-column-header-weight"),
    letterSpacing: v("type-column-header-tracking"),
    textTransform: "uppercase",
  },
  meta: { fontSize: v("type-meta-size"), lineHeight: v("type-meta-line") },
  code: {
    fontFamily: v("font-mono"),
    fontSize: v("type-code-size"),
    lineHeight: v("type-code-line"),
    fontVariantNumeric: "slashed-zero tabular-nums",
  },
  kpi: {
    fontSize: v("type-kpi-size"),
    lineHeight: v("type-kpi-line"),
    fontWeight: v("type-kpi-weight"),
    fontVariantNumeric: "tabular-nums",
  },
  badge: { fontSize: v("type-badge-size"), lineHeight: v("type-badge-line"), fontWeight: v("type-badge-weight") },
} as const satisfies Record<string, CSSProperties>;

const TONE_FAMILY_PREFIX: Record<Tone, string> = {
  critical: "severity-critical",
  high: "severity-high",
  warning: "severity-warning",
  informational: "severity-informational",
  healthy: "severity-healthy",
  neutral: "severity-neutral",
  pending: "state-pending",
  approved: "state-approved",
  denied: "state-denied",
  blocked: "state-blocked",
  stale: "state-stale",
  unknown: "state-unknown",
};

/** The colour of a tone, for the rare mark that is not a StatusBadge (a
 *  dot beside words). The tone itself still comes from `statusTone`. */
export function toneColor(tone: Tone, part: "fg" | "bg" | "border" = "fg"): string {
  return `var(--${TONE_FAMILY_PREFIX[tone]}-${part})`;
}
