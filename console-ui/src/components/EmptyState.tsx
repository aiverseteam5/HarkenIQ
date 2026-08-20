import { type CSSProperties } from "react";

interface Props {
  title: string;
  description?: string;
  actionLabel?: string;
  onAction?: () => void;
  icon?: string;
}

const containerStyle: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  justifyContent: "center",
  padding: "3rem 1.5rem",
  textAlign: "center",
  minHeight: 200,
};

const iconStyle: CSSProperties = {
  fontSize: "2rem",
  marginBottom: "0.75rem",
  opacity: 0.6,
};

const titleStyle: CSSProperties = {
  fontSize: "1rem",
  fontWeight: 600,
  color: "var(--text-primary)",
  marginBottom: "0.375rem",
};

const descStyle: CSSProperties = {
  fontSize: "0.8125rem",
  color: "var(--text-secondary)",
  maxWidth: 360,
  marginBottom: "1rem",
};

export default function EmptyState({ title, description, actionLabel, onAction, icon }: Props) {
  return (
    <div style={containerStyle}>
      {icon && <div style={iconStyle}>{icon}</div>}
      <div style={titleStyle}>{title}</div>
      {description && <div style={descStyle}>{description}</div>}
      {actionLabel && onAction && (
        <button className="btn btn-primary" onClick={onAction}>
          {actionLabel}
        </button>
      )}
    </div>
  );
}
