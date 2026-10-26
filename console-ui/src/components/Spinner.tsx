import { type CSSProperties } from "react";

const sizes = { sm: 16, md: 24, lg: 36 } as const;

interface Props {
  size?: "sm" | "md" | "lg";
  /** The spinner's own announcement. `null` when the spinner sits inside
   *  something that already says what is loading (A34.8), so a screen
   *  reader hears one message, not two. */
  label?: string | null;
}

export default function Spinner({ size = "md", label = "Loading" }: Props) {
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
      {label === null ? (
        <span aria-hidden="true" style={style} />
      ) : (
        <span role="status" aria-label={label} style={style} />
      )}
    </>
  );
}
