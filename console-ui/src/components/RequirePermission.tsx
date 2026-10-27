import { type ReactNode } from "react";
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
    <div className="access-denied">
      <div className="access-denied__code" aria-hidden="true">403</div>
      <div className="access-denied__title">You don't have access to this page</div>
      <p className="access-denied__body">
        Your role{user?.role ? ` (${user.role.replace(/_/g, " ")})` : ""} does
        not include this area. A tenant owner can grant it, or extend a custom
        role with the matching permission.
      </p>
    </div>
  );
}
