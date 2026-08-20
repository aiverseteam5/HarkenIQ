import { useParams } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";

export default function InvoiceDetail() {
  const { id } = useParams();
  return (
    <div>
      <PageHeader
        title={`Invoice ${id ?? ""}`}
        breadcrumbs={[
          { label: "HarkenIQ" },
          { label: "Billing", href: "/billing" },
          { label: `Invoice ${id ?? ""}` },
        ]}
      />
      <div className="card">
        <EmptyState
          title="Coming soon"
          description="Invoice line items, payment status, and PDF download will be available here."
          icon="&#x2B22;"
        />
      </div>
    </div>
  );
}
