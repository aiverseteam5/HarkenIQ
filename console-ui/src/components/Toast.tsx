import { type CSSProperties } from "react";
import { type ToastItem, type ToastVariant } from "./useToast";

interface Props {
  toasts: ToastItem[];
  onDismiss: (id: number) => void;
}

const containerStyle: CSSProperties = {
  position: "fixed",
  top: "1rem",
  right: "1rem",
  zIndex: 3000,
  display: "flex",
  flexDirection: "column",
  gap: "0.5rem",
  pointerEvents: "none",
};

const variantStyles: Record<ToastVariant, CSSProperties> = {
  success: {
    background: "var(--status-ok)",
    color: "#fff",
  },
  error: {
    background: "var(--status-critical)",
    color: "#fff",
  },
  info: {
    background: "var(--text-primary)",
    color: "#fff",
  },
};

const toastStyle: CSSProperties = {
  padding: "0.625rem 1rem",
  borderRadius: "var(--radius-md)",
  fontSize: "0.8125rem",
  fontWeight: 500,
  boxShadow: "var(--shadow-md)",
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: "0.75rem",
  pointerEvents: "auto",
  maxWidth: 380,
  animation: "hiq-toast-in 200ms ease",
};

const closeBtnStyle: CSSProperties = {
  background: "none",
  border: "none",
  color: "inherit",
  cursor: "pointer",
  opacity: 0.7,
  fontSize: "1rem",
  padding: "0.125rem",
  lineHeight: 1,
};

export default function Toast({ toasts, onDismiss }: Props) {
  if (toasts.length === 0) return null;

  return (
    <>
      <style>{`
        @keyframes hiq-toast-in {
          from { opacity: 0; transform: translateX(2rem); }
          to { opacity: 1; transform: translateX(0); }
        }
      `}</style>
      <div style={containerStyle} aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} style={{ ...toastStyle, ...variantStyles[t.variant] }} role="alert">
            <span>{t.message}</span>
            <button
              style={closeBtnStyle}
              onClick={() => onDismiss(t.id)}
              aria-label="Dismiss"
            >
              &#x2715;
            </button>
          </div>
        ))}
      </div>
    </>
  );
}
