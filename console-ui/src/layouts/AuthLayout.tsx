import { type CSSProperties } from "react";
import { Outlet } from "react-router-dom";

const containerStyle: CSSProperties = {
  minHeight: "100vh",
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  justifyContent: "center",
  background: "var(--bg-primary)",
  padding: "2rem",
};

const logoStyle: CSSProperties = {
  fontSize: "1.25rem",
  fontWeight: 700,
  color: "var(--text-primary)",
  marginBottom: "2rem",
  display: "flex",
  alignItems: "center",
  gap: "0.5rem",
};

const cardStyle: CSSProperties = {
  background: "var(--bg-card)",
  border: "1px solid var(--border-color)",
  borderRadius: "var(--radius-lg)",
  boxShadow: "var(--shadow-md)",
  padding: "2rem",
  width: "100%",
  maxWidth: 400,
};

export default function AuthLayout() {
  return (
    <div style={containerStyle}>
      <div style={logoStyle}>
        <span style={{ fontSize: "1.5rem" }}>&#x2B22;</span>
        HarkenIQ
      </div>
      <div style={cardStyle}>
        <Outlet />
      </div>
    </div>
  );
}
