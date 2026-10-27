import { type ToastItem } from "./useToast";
import IconButton from "./IconButton";

interface Props {
  toasts: ToastItem[];
  onDismiss: (id: number) => void;
}

export default function Toast({ toasts, onDismiss }: Props) {
  // A34.8: the live region is ALWAYS in the document. It used to mount
  // together with its first message, and a region that appears already
  // holding its text is often not announced at all -- so the one live
  // region in the Console frequently said nothing. An error interrupts
  // (alert); everything else waits its turn (status).
  return (
    <div className="toast-region" aria-live="polite" data-testid="toast-region">
      {toasts.map((t) => (
        <div key={t.id} className={`toast toast--${t.variant}`} role={t.variant === "error" ? "alert" : "status"}>
          <span>{t.message}</span>
          <IconButton icon="close" label="Dismiss" onClick={() => onDismiss(t.id)} />
        </div>
      ))}
    </div>
  );
}
