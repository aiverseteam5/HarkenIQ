interface Props {
  title: string;
  description?: string;
  actionLabel?: string;
  onAction?: () => void;
  /** A decorative glyph. Hidden from assistive technology: the title says
   *  what is empty. */
  icon?: string;
}

export default function EmptyState({ title, description, actionLabel, onAction, icon }: Props) {
  return (
    <div className="empty-state">
      {icon && (
        <div className="empty-state__icon" aria-hidden="true">
          {icon}
        </div>
      )}
      <div className="empty-state__title">{title}</div>
      {description && <div className="empty-state__description">{description}</div>}
      {actionLabel && onAction && (
        <button type="button" className="btn btn-primary" onClick={onAction}>
          {actionLabel}
        </button>
      )}
    </div>
  );
}
