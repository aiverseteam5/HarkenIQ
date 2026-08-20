import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";

export default function Dashboard() {
  return (
    <div>
      <PageHeader
        title="Dashboard"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Dashboard" }]}
      />
      <div className="card">
        <EmptyState
          title="Coming in R2b"
          description="Fleet-wide metrics, incident trends, and operational summaries will appear here."
          icon="&#x25A6;"
        />
      </div>
    </div>
  );
}
