import { type CSSProperties } from "react";

type Variant = "success" | "warning" | "critical" | "info" | "neutral";
type Size = "sm" | "md";

interface Props {
  status: string;
  variant?: Variant;
  size?: Size;
}

const variantMap: Record<Variant, { bg: string; color: string; dot: string }> = {
  success: { bg: "var(--status-ok-bg)", color: "var(--status-ok)", dot: "var(--status-ok)" },
  warning: { bg: "var(--status-warning-bg)", color: "var(--status-warning)", dot: "var(--status-warning)" },
  critical: { bg: "var(--status-critical-bg)", color: "var(--status-critical)", dot: "var(--status-critical)" },
  info: { bg: "var(--status-info-bg)", color: "var(--status-info)", dot: "var(--status-info)" },
  neutral: { bg: "var(--status-neutral-bg)", color: "var(--status-neutral)", dot: "var(--status-neutral)" },
};

export default function StatusBadge({ status, variant = "neutral", size = "md" }: Props) {
  const v = variantMap[variant];
  const isSmall = size === "sm";

  const style: CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    gap: isSmall ? "0.25rem" : "0.375rem",
    padding: isSmall ? "0.125rem 0.5rem" : "0.1875rem 0.625rem",
    fontSize: isSmall ? "0.6875rem" : "0.75rem",
    fontWeight: 600,
    letterSpacing: "0.02em",
    borderRadius: "999px",
    background: v.bg,
    color: v.color,
    lineHeight: 1.4,
    whiteSpace: "nowrap",
  };

  const dotStyle: CSSProperties = {
    width: isSmall ? 5 : 6,
    height: isSmall ? 5 : 6,
    borderRadius: "50%",
    background: v.dot,
    flexShrink: 0,
  };

  return (
    <span style={style}>
      <span style={dotStyle} aria-hidden="true" />
      {status}
    </span>
  );
}
