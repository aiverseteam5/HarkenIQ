import type { ButtonHTMLAttributes } from "react";
import Icon, { type UiGlyph } from "./Icon";

/** A35.6: a control drawn as a glyph. It always carries an accessible name
 *  (`label`), because the glyph alone says nothing to a screen reader, and
 *  its target never shrinks below 24px (WCAG 2.5.8). */

interface Props extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  icon: UiGlyph;
  label: string;
  /** On dark chrome (the sidebar) the glyph and its hover invert. */
  inverse?: boolean;
}

export default function IconButton({ icon, label, inverse = false, className, type = "button", ...rest }: Props) {
  const classes = ["icon-btn", inverse ? "icon-btn--inverse" : "", className ?? ""].filter(Boolean).join(" ");
  return (
    <button type={type} className={classes} aria-label={label} {...rest}>
      <Icon name={icon} />
    </button>
  );
}
