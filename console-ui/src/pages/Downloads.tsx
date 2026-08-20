import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";

export default function Downloads() {
  return (
    <div>
      <PageHeader
        title="Downloads"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Administration" }, { label: "Downloads" }]}
      />
      <div className="card">
        <EmptyState
          title="Coming soon"
          description="Agent installers, CLI tools, and configuration templates available for download."
          icon="&#x2913;"
        />
      </div>
    </div>
  );
}
