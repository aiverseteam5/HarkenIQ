import { type CSSProperties, type ReactNode, useId, useRef } from "react";
import { useOverlay } from "../a11y/overlay";
import Spinner from "./Spinner";

interface Props {
  open?: boolean;
  title: string;
  message?: string;
  children?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: "danger" | "default";
  onConfirm: () => void;
  onCancel: () => void;
  loading?: boolean;
}

const overlayStyle: CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "var(--bg-overlay)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 2000,
};

const cardStyle: CSSProperties = {
  background: "var(--bg-card)",
  borderRadius: "var(--radius-lg)",
  boxShadow: "var(--shadow-lg)",
  padding: "1.5rem",
  maxWidth: 420,
  width: "90vw",
};

const titleStyle: CSSProperties = {
  fontSize: "1rem",
  fontWeight: 600,
  margin: "0 0 0.5rem 0",
};

const messageStyle: CSSProperties = {
  fontSize: "0.875rem",
  color: "var(--text-secondary)",
  lineHeight: 1.5,
  margin: "0 0 1.25rem 0",
};

const actionsStyle: CSSProperties = {
  display: "flex",
  justifyContent: "flex-end",
  gap: "0.5rem",
};

export default function ConfirmDialog({
  open = true,
  title,
  message,
  children,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  variant = "default",
  onConfirm,
  onCancel,
  loading,
}: Props) {
  // A34.8: the dialog takes the keyboard while open. Focus starts in the
  // first field when the dialog asks for input, and otherwise on Cancel --
  // the choice that changes nothing -- so an Enter meant for the page behind
  // can never confirm a governance action by accident. Ids are per
  // instance: two dialogs on one page no longer share `confirm-title`.
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const messageId = useId();
  useOverlay(dialogRef, open, {
    onEscape: onCancel,
    initialFocus: () =>
      dialogRef.current?.querySelector<HTMLElement>(
        "input:not([disabled]), select:not([disabled]), textarea:not([disabled])",
      ) ?? cancelRef.current,
  });

  if (!open) return null;

  return (
    <div style={overlayStyle} onClick={onCancel} role="presentation">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={messageId}
        tabIndex={-1}
        data-focus-target=""
        style={cardStyle}
        onClick={(e) => e.stopPropagation()}
      >
        <h3 id={titleId} style={titleStyle}>{title}</h3>
        <div id={messageId} style={messageStyle}>{message ?? children}</div>
        <div style={actionsStyle}>
          <button ref={cancelRef} type="button" className="btn" onClick={onCancel} disabled={loading}>
            {cancelLabel}
          </button>
          <button
            type="button"
            className={`btn ${variant === "danger" ? "btn-danger" : "btn-primary"}`}
            onClick={onConfirm}
            disabled={loading}
          >
            {loading && <Spinner size="sm" />}
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
