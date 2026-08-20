import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";

export default function TenantSettings() {
  return (
    <div>
      <PageHeader
        title="Settings"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Administration" }, { label: "Settings" }]}
      />
      <div className="card">
        <EmptyState
          title="Coming soon"
          description="Tenant configuration, notification preferences, and integration settings."
          icon="&#x2338;"
        />
      </div>
    </div>
  );
}
