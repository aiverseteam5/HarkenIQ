import type { ReactNode } from "react";
import Icon from "./Icon";
import type { Tone } from "../viewModel/status";

/** A35.6: a notice inside the page (stale data, a partial load, a warning a
 *  section owes its reader). Its tone is drawn in colour and shape, and its
 *  words carry the message. It announces nothing by itself: pass `role`
 *  when the notice reports a change the reader did not ask for. */

interface Props {
  tone: Tone;
  title?: string;
  role?: "status" | "alert";
  className?: string;
  children: ReactNode;
}

export default function Banner({ tone, title, role, className, children }: Props) {
  return (
    <div className={className ? `banner ${className}` : "banner"} data-tone={tone} role={role}>
      <Icon name={`tone-${tone}`} className="banner__glyph" />
      <div>
        {title && <div className="banner__title">{title}</div>}
        {children}
      </div>
    </div>
  );
}
