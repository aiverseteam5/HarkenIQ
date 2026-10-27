interface Props {
  size?: "sm" | "md" | "lg";
  /** The spinner's own announcement. `null` when the spinner sits inside
   *  something that already says what is loading (A34.8), so a screen
   *  reader hears one message, not two. */
  label?: string | null;
}

/** A35.6: the one loading mark. Its motion is a state, not decoration, and
 *  it stops for anyone who asked for reduced motion (global.css). */
export default function Spinner({ size = "md", label = "Loading" }: Props) {
  const className = `spinner spinner--${size}`;
  return label === null ? (
    <span aria-hidden="true" className={className} />
  ) : (
    <span role="status" aria-label={label} className={className} />
  );
}
