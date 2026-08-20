import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";

export default function ReleaseManagement() {
  return (
    <div>
      <PageHeader
        title="Releases"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Administration" }, { label: "Releases" }]}
      />
      <div className="card">
        <EmptyState
          title="Coming soon"
          description="Agent and service release management, rollout tracking, and version control."
          icon="&#x2B06;"
        />
      </div>
    </div>
  );
}
