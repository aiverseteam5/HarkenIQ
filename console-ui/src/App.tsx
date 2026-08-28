import { Navigate, Route, Routes, useLocation } from "react-router-dom";
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
import TenantSettings from "./pages/TenantSettings";
import Downloads from "./pages/Downloads";
import ApiKeys from "./pages/ApiKeys";
import ReportingAnalytics from "./pages/ReportingAnalytics";
import ImpersonationLog from "./pages/ImpersonationLog";
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

  if (!user) return null;

  if (!user.is_platform_user && user.tenant_id) {
    const path = location.pathname === "/" ? "/dashboard" : location.pathname;
    return <Navigate to={`/t/${user.tenant_id}${path}`} replace />;
  }
  return <Navigate to="/tenants" replace />;
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
        <Route path="/admin/impersonation" element={<ImpersonationLog />} />
        <Route path="/admin/support-access" element={<SupportAccessRequests />} />
        <Route path="/marketplace" element={<SkillMarketplace />} />

        {/* ── Tenant plane: context is the URL ───────────────
            Not a header, not localStorage. The tenant is visible,
            bookmarkable, survives the back button, and two tabs can
            hold two different tenants. */}
        <Route path="/t/:tenantId/dashboard" element={<Dashboard />} />
        <Route path="/t/:tenantId/users" element={<UserManagement />} />
        <Route path="/t/:tenantId/licenses" element={<LicenseManagement />} />
        <Route path="/t/:tenantId/fleet" element={<FleetOverview />} />
        <Route path="/t/:tenantId/reliability" element={<VendorReliability />} />
        <Route path="/t/:tenantId/approvals" element={<ApprovalQueue />} />
        <Route path="/t/:tenantId/agents" element={<AgentManagement />} />
        <Route path="/t/:tenantId/policies" element={<ApprovalPolicies />} />
        <Route path="/t/:tenantId/billing" element={<BillingDashboard />} />
        <Route path="/t/:tenantId/invoices/:id" element={<InvoiceDetail />} />
        <Route path="/t/:tenantId/usage" element={<UsageChargeback />} />
        <Route path="/t/:tenantId/support" element={<SupportTicketing />} />
        <Route path="/t/:tenantId/audit" element={<AuditLogs />} />
        <Route path="/t/:tenantId/reports" element={<ReportingAnalytics />} />
        <Route path="/t/:tenantId/settings" element={<TenantSettings />} />
        <Route path="/t/:tenantId/downloads" element={<Downloads />} />
        <Route path="/t/:tenantId/api-keys" element={<ApiKeys />} />

        {/* Bare tenant-plane paths from bookmarks and old links: send a
            tenant user into their own tenant, a platform user to the
            registry to choose one deliberately. */}
        <Route path="/*" element={<TenantPathRedirect />} />
      </Route>
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
