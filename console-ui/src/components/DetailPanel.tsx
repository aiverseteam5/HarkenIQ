import { type CSSProperties, type ReactNode, useEffect } from "react";

interface Props {
  open: boolean;
  onClose: () => void;
  title: string;
  subtitle?: string;
  width?: number;
  children: ReactNode;
}

const overlayStyle: CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "var(--bg-overlay)",
  zIndex: 1000,
  transition: "opacity var(--transition)",
};

const panelBase: CSSProperties = {
  position: "fixed",
  top: 0,
  right: 0,
  bottom: 0,
  background: "var(--bg-card)",
  boxShadow: "var(--shadow-lg)",
  zIndex: 1001,
  display: "flex",
  flexDirection: "column",
  transition: "transform 200ms ease",
};

const headerStyle: CSSProperties = {
  display: "flex",
  alignItems: "flex-start",
  justifyContent: "space-between",
  padding: "1.25rem 1.5rem 1rem",
  borderBottom: "1px solid var(--border-color)",
};

const bodyStyle: CSSProperties = {
  flex: 1,
  overflow: "auto",
  padding: "1.25rem 1.5rem",
};

const closeBtn: CSSProperties = {
  background: "none",
  border: "none",
  fontSize: "1.25rem",
  cursor: "pointer",
  color: "var(--text-secondary)",
  padding: "0.25rem",
  lineHeight: 1,
};

export default function DetailPanel({ open, onClose, title, subtitle, width = 480, children }: Props) {
  // Trap Escape key
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [open, onClose]);

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
      <div
        style={overlayStyle}
        onClick={onClose}
        aria-hidden="true"
      />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label={title}
        style={{
          ...panelBase,
          width,
          transform: open ? "translateX(0)" : `translateX(${width}px)`,
        }}
      >
        <div style={headerStyle}>
          <div>
            <h2 style={{ margin: 0, fontSize: "1.125rem" }}>{title}</h2>
            {subtitle && (
              <div style={{ fontSize: "0.8125rem", color: "var(--text-secondary)", marginTop: "0.25rem" }}>
                {subtitle}
              </div>
            )}
          </div>
          <button
            style={closeBtn}
            onClick={onClose}
            aria-label="Close panel"
          >
            &#x2715;
          </button>
        </div>
        <div style={bodyStyle}>{children}</div>
      </aside>
    </>
  );
}
