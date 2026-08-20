import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";

export default function AuditLogs() {
  return (
    <div>
      <PageHeader
        title="Audit Logs"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Operations" }, { label: "Audit Logs" }]}
      />
      <div className="card">
        <EmptyState
          title="Coming soon"
          description="Full audit trail of all console actions, searchable and exportable."
          icon="&#x2630;"
        />
      </div>
    </div>
  );
}
