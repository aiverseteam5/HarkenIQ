import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";

export default function ApprovalQueue() {
  return (
    <div>
      <PageHeader
        title="Approvals"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Fleet" }, { label: "Approvals" }]}
      />
      <div className="card">
        <EmptyState
          title="Coming soon"
          description="Pending approval actions from all sites will be queued here for review."
          icon="&#x2714;"
        />
      </div>
    </div>
  );
}
