import { type ReactNode, type CSSProperties } from "react";

interface Breadcrumb {
  label: string;
  href?: string;
}

interface Props {
  title: string;
  breadcrumbs?: Breadcrumb[];
  actions?: ReactNode;
}

const wrapperStyle: CSSProperties = {
  marginBottom: "1.5rem",
};

const crumbsStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "0.375rem",
  fontSize: "0.75rem",
  color: "var(--text-muted)",
  marginBottom: "0.375rem",
};

const separatorStyle: CSSProperties = {
  color: "var(--text-muted)",
};

const rowStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: "1rem",
};

const titleStyle: CSSProperties = {
  fontSize: "1.375rem",
  fontWeight: 600,
  margin: 0,
  color: "var(--text-primary)",
};

export default function PageHeader({ title, breadcrumbs, actions }: Props) {
  return (
    <div style={wrapperStyle}>
      {breadcrumbs && breadcrumbs.length > 0 && (
        <nav aria-label="Breadcrumb" style={crumbsStyle}>
          {breadcrumbs.map((bc, i) => (
            <span key={i} style={{ display: "flex", alignItems: "center", gap: "0.375rem" }}>
              {i > 0 && <span style={separatorStyle}>/</span>}
              {bc.href ? (
                <a href={bc.href} style={{ color: "var(--accent)", textDecoration: "none" }}>
                  {bc.label}
                </a>
              ) : (
                <span>{bc.label}</span>
              )}
            </span>
          ))}
        </nav>
      )}
      <div style={rowStyle}>
        <h1 style={titleStyle}>{title}</h1>
        {actions && <div className="flex gap-sm">{actions}</div>}
      </div>
    </div>
  );
}
