import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";

export default function AdminDashboard() {
  return (
    <div>
      <PageHeader
        title="Admin Dashboard"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Administration" }, { label: "Dashboard" }]}
      />
      <div className="card">
        <EmptyState
          title="Coming soon"
          description="Platform-wide operational metrics and administrative controls will be available here."
          icon="&#x2699;"
        />
      </div>
    </div>
  );
}
