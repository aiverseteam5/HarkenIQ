import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";

export default function PlatformHealth() {
  return (
    <div>
      <PageHeader
        title="Platform Health"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Administration" }, { label: "Platform Health" }]}
      />
      <div className="card">
        <EmptyState
          title="Coming soon"
          description="Service health, uptime metrics, and infrastructure monitoring for the HarkenIQ platform."
          icon="&#x2665;"
        />
      </div>
    </div>
  );
}
