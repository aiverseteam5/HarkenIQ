import { type CSSProperties } from "react";

/* ── Types ────────────────────────────────────────── */

export interface SidebarItem {
  key: string;
  label: string;
  icon?: string;
  badge?: number;
}

export interface SidebarSection {
  label: string;
  items: SidebarItem[];
}

interface Props {
  sections: SidebarSection[];
  activeItem: string;
  onNavigate: (key: string) => void;
  user: { name: string; email: string; role: string };
  onLogout?: () => void;
}

/* ── Styles ───────────────────────────────────────── */

const sidebarStyle: CSSProperties = {
  width: "var(--sidebar-width)",
  minWidth: "var(--sidebar-width)",
  height: "100vh",
  background: "var(--sidebar-bg)",
  color: "var(--sidebar-text)",
  display: "flex",
  flexDirection: "column",
  position: "fixed",
  top: 0,
  left: 0,
  zIndex: 100,
  overflowY: "auto",
  overflowX: "hidden",
};

const logoStyle: CSSProperties = {
  padding: "1.25rem 1rem 0.75rem",
  fontSize: "1rem",
  fontWeight: 700,
  color: "#fff",
  letterSpacing: "-0.02em",
  display: "flex",
  alignItems: "center",
  gap: "0.5rem",
};

const sectionLabelStyle: CSSProperties = {
  padding: "1rem 1rem 0.375rem",
  fontSize: "0.625rem",
  fontWeight: 700,
  textTransform: "uppercase",
  letterSpacing: "0.08em",
  color: "rgba(168, 177, 193, 0.6)",
};

const itemStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "0.625rem",
  padding: "0.4375rem 1rem",
  fontSize: "0.8125rem",
  color: "var(--sidebar-text)",
  cursor: "pointer",
  border: "none",
  background: "transparent",
  width: "100%",
  textAlign: "left",
  fontFamily: "inherit",
  borderRadius: 0,
  transition: "background var(--transition), color var(--transition)",
};

const itemActiveStyle: CSSProperties = {
  ...itemStyle,
  color: "var(--sidebar-active)",
  background: "var(--sidebar-active-bg)",
};

const badgeStyle: CSSProperties = {
  marginLeft: "auto",
  background: "var(--accent)",
  color: "#fff",
  fontSize: "0.6875rem",
  fontWeight: 700,
  padding: "0.0625rem 0.4375rem",
  borderRadius: "999px",
  lineHeight: 1.5,
};

const userAreaStyle: CSSProperties = {
  marginTop: "auto",
  padding: "0.75rem 1rem",
  borderTop: "1px solid rgba(255,255,255,0.08)",
  display: "flex",
  alignItems: "center",
  gap: "0.625rem",
};

const avatarStyle: CSSProperties = {
  width: 32,
  height: 32,
  borderRadius: "50%",
  background: "var(--accent)",
  color: "#fff",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  fontSize: "0.75rem",
  fontWeight: 700,
  flexShrink: 0,
};

const roleTagStyle: CSSProperties = {
  fontSize: "0.625rem",
  fontWeight: 600,
  textTransform: "uppercase",
  letterSpacing: "0.04em",
  color: "rgba(168, 177, 193, 0.7)",
};

/* ── Component ────────────────────────────────────── */

export default function Sidebar({ sections, activeItem, onNavigate, user, onLogout }: Props) {
  const initials = user.name
    .split(" ")
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();

  return (
    <nav style={sidebarStyle} aria-label="Main navigation">
      <div style={logoStyle}>
        <span style={{ fontSize: "1.25rem" }}>&#x2B22;</span>
        HarkenIQ
      </div>

      {sections.map((section) => (
        <div key={section.label}>
          <div style={sectionLabelStyle}>{section.label}</div>
          {section.items.map((item) => (
            <button
              key={item.key}
              style={activeItem === item.key ? itemActiveStyle : itemStyle}
              onClick={() => onNavigate(item.key)}
              onMouseEnter={(e) => {
                if (activeItem !== item.key) {
                  e.currentTarget.style.background = "var(--sidebar-hover-bg)";
                }
              }}
              onMouseLeave={(e) => {
                if (activeItem !== item.key) {
                  e.currentTarget.style.background = "transparent";
                }
              }}
              aria-current={activeItem === item.key ? "page" : undefined}
            >
              {item.icon && <span style={{ fontSize: "1rem", width: 20, textAlign: "center" }}>{item.icon}</span>}
              <span>{item.label}</span>
              {item.badge !== undefined && item.badge > 0 && (
                <span style={badgeStyle}>{item.badge}</span>
              )}
            </button>
          ))}
        </div>
      ))}

      <div style={userAreaStyle}>
        <div style={avatarStyle} aria-hidden="true">{initials}</div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: "0.8125rem", color: "#fff", fontWeight: 500 }} className="truncate">
            {user.name}
          </div>
          <div style={roleTagStyle}>{user.role.replace(/_/g, " ")}</div>
        </div>
        {onLogout && (
          <button
            onClick={onLogout}
            aria-label="Sign out"
            style={{
              background: "none",
              border: "none",
              color: "var(--sidebar-text)",
              cursor: "pointer",
              fontSize: "1rem",
              padding: "0.25rem",
            }}
            title="Sign out"
          >
            &#x23FB;
          </button>
        )}
      </div>
    </nav>
  );
}
