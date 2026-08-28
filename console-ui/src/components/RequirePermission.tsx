import { type CSSProperties, type ReactNode } from "react";
import { useLocation } from "react-router-dom";
import { useAuth } from "../useAuth";
import { canAccess, ruleFor } from "../permissions";

/**
 * Route-level reflection of the server's rules (spec S4).
 *
 * Nav filtering alone is not enough: routes are reachable by typed URL,
 * bookmark, or a stale link. Without this the page mounts, fires its
 * fetches, and shows an empty or half-broken screen while the server
 * returns 403 — which is how the QA-048 class of "silently empty page"
 * happens. Say plainly that access is denied instead.
 *
 * This is not a security boundary. The server is. This exists so the UI
 * does not lie about what the user can do.
 */

const wrapStyle: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  justifyContent: "center",
  minHeight: "60vh",
  gap: "0.75rem",
  textAlign: "center",
  padding: "2rem",
};

const codeStyle: CSSProperties = {
  fontSize: "3rem",
  fontWeight: 700,
  color: "var(--color-text-muted, #888)",
  lineHeight: 1,
};

const titleStyle: CSSProperties = { fontSize: "1.125rem", fontWeight: 600 };

const bodyStyle: CSSProperties = {
  color: "var(--color-text-muted, #888)",
  maxWidth: "34rem",
};

export default function RequirePermission({
  children,
}: {
  children: ReactNode;
}) {
  const { user } = useAuth();
  const location = useLocation();

  if (canAccess(user, ruleFor(location.pathname))) {
    return <>{children}</>;
  }

  return (
    <div style={wrapStyle}>
      <div style={codeStyle}>403</div>
      <div style={titleStyle}>You don't have access to this page</div>
      <p style={bodyStyle}>
        Your role{user?.role ? ` (${user.role.replace(/_/g, " ")})` : ""} does
        not include this area. A tenant owner can grant it, or extend a custom
        role with the matching permission.
      </p>
    </div>
  );
}
