import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";

export default function TenantManagement() {
  return (
    <div>
      <PageHeader
        title="Tenants"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Operations" }, { label: "Tenants" }]}
      />
      <div className="card">
        <EmptyState
          title="Coming soon"
          description="Tenant management, onboarding, and lifecycle controls will be available here."
          icon="&#x2302;"
        />
      </div>
    </div>
  );
}
