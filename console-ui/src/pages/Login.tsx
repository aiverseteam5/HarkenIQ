import { type CSSProperties } from "react";
import { useAuth } from "../useAuth";

const titleStyle: CSSProperties = {
  fontSize: "1.25rem",
  fontWeight: 600,
  textAlign: "center",
  marginBottom: "0.5rem",
};

const subtitleStyle: CSSProperties = {
  fontSize: "0.875rem",
  color: "var(--text-secondary)",
  textAlign: "center",
  marginBottom: "1.5rem",
};

const btnStyle: CSSProperties = {
  width: "100%",
  padding: "0.625rem",
  fontSize: "0.875rem",
  fontWeight: 600,
};

export default function Login() {
  const { login } = useAuth();

  return (
    <div>
      <h2 style={titleStyle}>Welcome back</h2>
      <p style={subtitleStyle}>Sign in to access the HarkenIQ Console.</p>
      <button
        className="btn btn-primary"
        style={btnStyle}
        onClick={() => void login()}
      >
        Sign in with HarkenIQ
      </button>
    </div>
  );
}
