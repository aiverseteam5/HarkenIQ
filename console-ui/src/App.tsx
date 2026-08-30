import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { redirectTargetFor } from "./permissions";
import { AuthProvider, useAuth } from "./useAuth";
import SidebarLayout from "./layouts/SidebarLayout";
import AuthLayout from "./layouts/AuthLayout";
import Spinner from "./components/Spinner";

/* ── Pages ────────────────────────────────────────── */

import Login from "./pages/Login";
import Callback from "./pages/Callback";
import Dashboard from "./pages/Dashboard";
import TenantManagement from "./pages/TenantManagement";
import UserManagement from "./pages/UserManagement";
import LicenseManagement from "./pages/LicenseManagement";
import FleetOverview from "./pages/FleetOverview";
import Incidents from "./pages/Incidents";
import Autonomy from "./pages/Autonomy";
import Organization from "./pages/Organization";
import AccessScope from "./pages/AccessScope";
import OperationalAgents from "./pages/OperationalAgents";
import Learning from "./pages/Learning";
import RiskExposure from "./pages/RiskExposure";
import VendorReliability from "./pages/VendorReliability";
import SkillMarketplace from "./pages/SkillMarketplace";
import ApprovalQueue from "./pages/ApprovalQueue";
import BillingDashboard from "./pages/BillingDashboard";
import InvoiceDetail from "./pages/InvoiceDetail";
import AdminBillingStats from "./pages/AdminBillingStats";
import SupportTicketing from "./pages/SupportTicketing";
import AuditLogs from "./pages/AuditLogs";
import AgentManagement from "./pages/AgentManagement";
import ApprovalPolicies from "./pages/ApprovalPolicies";
import AdminDashboard from "./pages/AdminDashboard";
import FeatureToggles from "./pages/FeatureToggles";
import ReleaseManagement from "./pages/ReleaseManagement";
import PlatformHealth from "./pages/PlatformHealth";
import Downloads from "./pages/Downloads";
import UsageChargeback from "./pages/UsageChargeback";
import SupportAccessRequests from "./pages/SupportAccessRequests";

/* ── Route guards ─────────────────────────────────── */

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, loading } = useAuth();

  if (loading) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100vh" }}>
        <Spinner size="lg" />
      </div>
    );
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  return <>{children}</>;
}

/**
 * Bare tenant-plane paths (`/audit`, `/billing`, a stale bookmark) have no
 * tenant in them. A tenant user has exactly one, so send them there and
 * they never see a chooser or type an id. A platform user has many, and
 * guessing for them is precisely the auto-select behaviour this
 * restructure removed — so they go to the registry and pick.
 */
function TenantPathRedirect() {
  const { user } = useAuth();
  const location = useLocation();

  const target = redirectTargetFor(user, location.pathname);
  if (!target) return null;
  return <Navigate to={target} replace />;
}

function RedirectIfAuth({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, loading } = useAuth();

  if (loading) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100vh" }}>
        <Spinner size="lg" />
      </div>
    );
  }

  if (isAuthenticated) {
    return <Navigate to="/dashboard" replace />;
  }

  return <>{children}</>;
}

/* ── App ──────────────────────────────────────────── */

function AppRoutes() {
  return (
    <Routes>
      {/* Root redirect */}
      <Route path="/" element={<Navigate to="/dashboard" replace />} />

      {/* Auth pages */}
      <Route
        element={
          <RedirectIfAuth>
            <AuthLayout />
          </RedirectIfAuth>
        }
      >
        <Route path="/login" element={<Login />} />
        <Route path="/callback" element={<Callback />} />
      </Route>

      {/* Protected pages with sidebar */}
      <Route
        element={
          <RequireAuth>
            <SidebarLayout />
          </RequireAuth>
        }
      >
        {/* ── Platform plane: no tenant context ──────────────
            Administering the platform is not administering any one
            tenant, so these paths carry no tenant id and the tenant
            context header does not render on them. */}
        <Route path="/tenants" element={<TenantManagement />} />
        <Route path="/admin" element={<AdminDashboard />} />
        <Route path="/admin/billing" element={<AdminBillingStats />} />
        <Route path="/admin/features" element={<FeatureToggles />} />
        <Route path="/admin/releases" element={<ReleaseManagement />} />
        <Route path="/admin/health" element={<PlatformHealth />} />
        {/* P0 2026-08-29: /admin/impersonation retired — the log had no
            writer anywhere in the backend (R-H3 lists impersonation as
            "if ever added"); a page implying a capability that does not
            exist is a trust bug. Returns with the capability, if ever. */}
        <Route path="/admin/support-access" element={<SupportAccessRequests />} />
        <Route path="/marketplace" element={<SkillMarketplace />} />

        {/* ── Tenant plane: context is the URL ───────────────
            Not a header, not localStorage. The tenant is visible,
            bookmarkable, survives the back button, and two tabs can
            hold two different tenants. */}
        <Route path="/t/:tenantId/dashboard" element={<Dashboard />} />
        <Route path="/t/:tenantId/users" element={<UserManagement />} />
        <Route path="/t/:tenantId/licenses" element={<LicenseManagement />} />
        <Route path="/t/:tenantId/incidents" element={<Incidents />} />
        <Route path="/t/:tenantId/fleet" element={<FleetOverview />} />
        <Route path="/t/:tenantId/risk" element={<RiskExposure />} />
        <Route path="/t/:tenantId/learning" element={<Learning />} />
        <Route path="/t/:tenantId/autonomy" element={<Autonomy />} />
        <Route path="/t/:tenantId/organization" element={<Organization />} />
        <Route path="/t/:tenantId/access-scope" element={<AccessScope />} />
        <Route path="/t/:tenantId/operational-agents" element={<OperationalAgents />} />
        <Route path="/t/:tenantId/reliability" element={<VendorReliability />} />
        <Route path="/t/:tenantId/approvals" element={<ApprovalQueue />} />
        <Route path="/t/:tenantId/agents" element={<AgentManagement />} />
        <Route path="/t/:tenantId/policies" element={<ApprovalPolicies />} />
        <Route path="/t/:tenantId/billing" element={<BillingDashboard />} />
        <Route path="/t/:tenantId/invoices/:id" element={<InvoiceDetail />} />
        <Route path="/t/:tenantId/usage" element={<UsageChargeback />} />
        <Route path="/t/:tenantId/support" element={<SupportTicketing />} />
        <Route path="/t/:tenantId/audit" element={<AuditLogs />} />
        {/* P0 2026-08-29 — three phantom pages retired (final assessment §3):
            /reports called four endpoints that exist nowhere in the repo;
            /settings was a mock whose Save was a 300ms timer storing
            nothing; /api-keys minted credentials no endpoint ever
            verified (get_by_hash has no production caller — service
            accounts replace the concept in P2). Each returns only when
            its backend is real. */}
        <Route path="/t/:tenantId/downloads" element={<Downloads />} />
        <Route path="/t/:tenantId/marketplace" element={<SkillMarketplace />} />

      </Route>

      {/* Bare tenant-plane paths from bookmarks and old links: send a
          tenant user into their own tenant, a platform user to the
          registry to choose one deliberately.

          D2 fix (P0 2026-08-29): OUTSIDE the SidebarLayout on purpose.
          Inside it, RequirePermission evaluated the STALE pathname's rule
          before the redirect could run — a platform_support login landed
          on a 403 for /dashboard instead of being sent to /tenants. The
          redirect's target route is fully guarded once it renders. */}
      <Route
        path="/*"
        element={
          <RequireAuth>
            <TenantPathRedirect />
          </RequireAuth>
        }
      />
    </Routes>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <AppRoutes />
    </AuthProvider>
  );
}
