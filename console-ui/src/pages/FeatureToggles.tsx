import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";

export default function FeatureToggles() {
  return (
    <div>
      <PageHeader
        title="Feature Toggles"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Administration" }, { label: "Feature Toggles" }]}
      />
      <div className="card">
        <EmptyState
          title="Coming soon"
          description="Feature flag management for rolling out capabilities per tenant."
          icon="&#x2692;"
        />
      </div>
    </div>
  );
}
