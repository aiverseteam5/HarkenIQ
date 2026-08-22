import { Navigate, Route, Routes } from "react-router-dom";
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
import UsageChargeback from "./pages/UsageChargeback";

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
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/tenants" element={<TenantManagement />} />
        <Route path="/users" element={<UserManagement />} />
        <Route path="/licenses" element={<LicenseManagement />} />
        <Route path="/fleet" element={<FleetOverview />} />
        <Route path="/approvals" element={<ApprovalQueue />} />
        <Route path="/agents" element={<AgentManagement />} />
        <Route path="/policies" element={<ApprovalPolicies />} />
        <Route path="/billing" element={<BillingDashboard />} />
        <Route path="/invoices/:id" element={<InvoiceDetail />} />
        <Route path="/usage" element={<UsageChargeback />} />
        <Route path="/admin/billing" element={<AdminBillingStats />} />
        <Route path="/support" element={<SupportTicketing />} />
        <Route path="/audit" element={<AuditLogs />} />
        <Route path="/reports" element={<ReportingAnalytics />} />
        <Route path="/admin" element={<AdminDashboard />} />
        <Route path="/admin/features" element={<FeatureToggles />} />
        <Route path="/admin/releases" element={<ReleaseManagement />} />
        <Route path="/admin/health" element={<PlatformHealth />} />
        <Route path="/settings" element={<TenantSettings />} />
        <Route path="/downloads" element={<Downloads />} />
        <Route path="/api-keys" element={<ApiKeys />} />
      </Route>

      {/* 404 fallback */}
      <Route path="*" element={<Navigate to="/dashboard" replace />} />
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
