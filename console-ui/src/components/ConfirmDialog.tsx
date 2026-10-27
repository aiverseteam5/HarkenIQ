import { type ReactNode, useId, useRef } from "react";
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
    <div className="dialog-scrim" onClick={onCancel} role="presentation">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={messageId}
        tabIndex={-1}
        data-focus-target=""
        className="dialog"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 id={titleId} className="dialog__title">{title}</h3>
        <div id={messageId} className="dialog__body">{message ?? children}</div>
        <div className="dialog__actions">
          <button ref={cancelRef} type="button" className="btn" onClick={onCancel} disabled={loading}>
            {cancelLabel}
          </button>
          <button
            type="button"
            className={`btn ${variant === "danger" ? "btn-danger" : "btn-primary"}`}
            onClick={onConfirm}
            disabled={loading}
            aria-busy={loading || undefined}
          >
            {loading && <Spinner size="sm" label={null} />}
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
