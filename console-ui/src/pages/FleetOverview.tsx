import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";

export default function FleetOverview() {
  return (
    <div>
      <PageHeader
        title="Fleet Overview"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Fleet" }, { label: "Overview" }]}
      />
      <div className="card">
        <EmptyState
          title="Coming soon"
          description="Cross-site fleet health, device inventory, and incident summaries will be available here."
          icon="&#x2318;"
        />
      </div>
    </div>
  );
}
