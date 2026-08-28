import { type CSSProperties } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import Sidebar, { type SidebarSection } from "../components/Sidebar";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";
import { useAuth } from "../useAuth";
import { ROUTE_ACCESS, canAccess } from "../permissions";
import RequirePermission from "../components/RequirePermission";
import TenantSelector from "../components/TenantSelector";

/* ── Navigation structure ─────────────────────────── */

const NAV_SECTIONS: SidebarSection[] = [
  {
    label: "Fleet",
    items: [
      { key: "/dashboard", label: "Dashboard", icon: "\u25A6" },
      { key: "/fleet", label: "Fleet Overview", icon: "\u2318" },
      { key: "/reliability", label: "Vendor Reliability", icon: "\u2696" },
      { key: "/approvals", label: "Approvals", icon: "\u2714" },
      { key: "/agents", label: "Agents", icon: "\u2699" },
    ],
  },
  {
    label: "Operations",
    items: [
      { key: "/policies", label: "Policies", icon: "\u2696" },
      { key: "/tenants", label: "Tenants", icon: "\u2302" },
      { key: "/users", label: "Users", icon: "\u263A" },
      { key: "/licenses", label: "Licenses", icon: "\u26BF" },
      { key: "/support", label: "Support", icon: "\u2709" },
      { key: "/audit", label: "Audit Logs", icon: "\u2630" },
      { key: "/reports", label: "Reports", icon: "\u2261" },
      { key: "/marketplace", label: "Skill Marketplace", icon: "\u2606" },
    ],
  },
  {
    label: "Billing",
    items: [
      { key: "/billing", label: "Billing", icon: "\u2B22" },
      { key: "/usage", label: "Usage & Chargeback", icon: "\u2261" },
      { key: "/admin/billing", label: "Billing Admin", icon: "\u2211" },
    ],
  },
  {
    label: "Administration",
    items: [
      { key: "/admin", label: "Admin Dashboard", icon: "\u2699" },
      { key: "/admin/features", label: "Feature Toggles", icon: "\u2692" },
      { key: "/admin/releases", label: "Releases", icon: "\u2B06" },
      { key: "/admin/health", label: "Platform Health", icon: "\u2665" },
      { key: "/settings", label: "Settings", icon: "\u2338" },
      { key: "/downloads", label: "Downloads", icon: "\u2913" },
      { key: "/api-keys", label: "API Keys", icon: "\u26BF" },
      { key: "/admin/impersonation", label: "Impersonation Log", icon: "\u263A" },
    ],
  },
];

/* ── Styles ───────────────────────────────────────── */

const layoutStyle: CSSProperties = {
  display: "flex",
  minHeight: "100vh",
};

const contentStyle: CSSProperties = {
  marginLeft: "var(--sidebar-width)",
  flex: 1,
  padding: "1.5rem 2rem",
  minWidth: 0,
  background: "var(--bg-primary)",
  overflow: "auto",
};

/* ── Component ────────────────────────────────────── */

export default function SidebarLayout() {
  const location = useLocation();
  const navigate = useNavigate();
  const { user, logout } = useAuth();
  const { toasts, dismiss } = useToast();

  // Determine active nav item from current path
  const activeItem = NAV_SECTIONS
    .flatMap((s) => s.items)
    .map((i) => i.key)
    .sort((a, b) => b.length - a.length) // longest match first
    .find((key) => location.pathname.startsWith(key)) ?? "/dashboard";

  // Spec S4: the UI reflects server-side permissions. Sections whose items
  // are all filtered out disappear rather than leaving an empty heading.
  const visibleSections = NAV_SECTIONS.map((section) => ({
    ...section,
    items: section.items.filter((item) =>
      canAccess(user, ROUTE_ACCESS[item.key]),
    ),
  })).filter((section) => section.items.length > 0);

  return (
    <div style={layoutStyle}>
      <Sidebar
        sections={visibleSections}
        activeItem={activeItem}
        onNavigate={(key) => navigate(key)}
        user={{
          name: user?.name ?? "Unknown",
          email: user?.email ?? "",
          role: user?.role ?? "viewer",
        }}
        onLogout={logout}
      />
      <main style={contentStyle}>
        <TenantSelector />
        {/* Inside the layout on purpose: a denied page keeps the sidebar so
            the user can navigate away, rather than being dropped onto a
            bare 403 with no way back. */}
        <RequirePermission>
          <Outlet />
        </RequirePermission>
      </main>
      <Toast toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}
