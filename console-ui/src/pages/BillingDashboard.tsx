import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";

export default function BillingDashboard() {
  return (
    <div>
      <PageHeader
        title="Billing"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Billing" }, { label: "Overview" }]}
      />
      <div className="card">
        <EmptyState
          title="Coming soon"
          description="Subscription details, invoices, and usage summaries will be available here."
          icon="&#x2B22;"
        />
      </div>
    </div>
  );
}
