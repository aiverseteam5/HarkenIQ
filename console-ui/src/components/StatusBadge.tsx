import Icon from "./Icon";
import type { Tone } from "../viewModel/status";

/** A35.5: a status, said in words, with its tone in colour AND shape.
 *
 *  `tone` comes from `statusTone(domain, value)` and nowhere else: the
 *  token lint refuses any other expression, so no page can decide a
 *  severity locally again. The glyph is decorative; the words are the
 *  status. */

interface Props {
  status: string;
  tone: Tone;
  size?: "sm" | "md";
}

export default function StatusBadge({ status, tone, size = "md" }: Props) {
  return (
    <span className={size === "sm" ? "status-badge status-badge--sm" : "status-badge"} data-tone={tone}>
      <Icon name={`tone-${tone}`} size={12} className="status-badge__glyph" />
      {status}
    </span>
  );
}
