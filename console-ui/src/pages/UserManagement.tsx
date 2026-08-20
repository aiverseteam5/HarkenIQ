import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";

export default function UserManagement() {
  return (
    <div>
      <PageHeader
        title="Users"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Operations" }, { label: "Users" }]}
      />
      <div className="card">
        <EmptyState
          title="Coming soon"
          description="User accounts, role assignments, and MFA management will be available here."
          icon="&#x263A;"
        />
      </div>
    </div>
  );
}
