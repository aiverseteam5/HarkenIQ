import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";

export default function LicenseManagement() {
  return (
    <div>
      <PageHeader
        title="Licenses"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Operations" }, { label: "Licenses" }]}
      />
      <div className="card">
        <EmptyState
          title="Coming soon"
          description="License allocation, usage tracking, and tier management will be available here."
          icon="&#x26BF;"
        />
      </div>
    </div>
  );
}
