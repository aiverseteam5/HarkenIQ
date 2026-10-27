import { type ReactNode, useEffect, useId, useRef } from "react";
import { useOverlay } from "../a11y/overlay";
import IconButton from "./IconButton";

interface Props {
  open: boolean;
  onClose: () => void;
  title: string;
  subtitle?: string;
  width?: number;
  children: ReactNode;
}

export default function DetailPanel({ open, onClose, title, subtitle, width = 480, children }: Props) {
  // A34.8: focus moves in on open, Tab stays inside, Escape closes only the
  // topmost overlay, and focus returns to whatever opened the panel.
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useOverlay(panelRef, open, { onEscape: onClose });

  // Prevent body scroll when open
  useEffect(() => {
    if (open) {
      document.body.style.overflow = "hidden";
    } else {
      document.body.style.overflow = "";
    }
    return () => {
      document.body.style.overflow = "";
    };
  }, [open]);

  if (!open) return null;

  return (
    <>
      <div className="drawer-scrim" onClick={onClose} aria-hidden="true" />
      {/* A34.8: a div, not an aside -- `aside` may not take the dialog role
          (axe: aria-allowed-role), so assistive technology was told two
          contradictory things about what this is. */}
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        data-focus-target=""
        className="drawer"
        style={{ width }}
      >
        <div className="drawer__header">
          <div>
            <h2 id={titleId} className="drawer__title">{title}</h2>
            {subtitle && <div className="drawer__subtitle">{subtitle}</div>}
          </div>
          <IconButton icon="close" label="Close panel" onClick={onClose} />
        </div>
        <div className="drawer__body">{children}</div>
      </div>
    </>
  );
}
