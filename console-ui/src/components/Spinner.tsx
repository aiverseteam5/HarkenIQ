import { type CSSProperties } from "react";

const sizes = { sm: 16, md: 24, lg: 36 } as const;

export default function Spinner({ size = "md" }: { size?: "sm" | "md" | "lg" }) {
  const px = sizes[size];
  const style: CSSProperties = {
    width: px,
    height: px,
    border: `${Math.max(2, px / 8)}px solid var(--border-color)`,
    borderTopColor: "var(--accent)",
    borderRadius: "50%",
    animation: "hiq-spin 0.6s linear infinite",
    display: "inline-block",
  };

  return (
    <>
      <style>{`@keyframes hiq-spin { to { transform: rotate(360deg); } }`}</style>
      <span role="status" aria-label="Loading" style={style} />
    </>
  );
}
