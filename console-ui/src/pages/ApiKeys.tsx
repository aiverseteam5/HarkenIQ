import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";

export default function ApiKeys() {
  return (
    <div>
      <PageHeader
        title="API Keys"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Administration" }, { label: "API Keys" }]}
      />
      <div className="card">
        <EmptyState
          title="Coming soon"
          description="API key generation, rotation, and scope management for programmatic access."
          icon="&#x26BF;"
        />
      </div>
    </div>
  );
}
