import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";

export default function AdminBillingStats() {
  return (
    <div>
      <PageHeader
        title="Billing Administration"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Billing" }, { label: "Admin" }]}
      />
      <div className="card">
        <EmptyState
          title="Coming soon"
          description="Cross-tenant billing analytics, revenue metrics, and payment processing stats will be available here."
          icon="&#x2211;"
        />
      </div>
    </div>
  );
}
