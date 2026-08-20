import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";

export default function SupportTicketing() {
  return (
    <div>
      <PageHeader
        title="Support"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Operations" }, { label: "Support" }]}
      />
      <div className="card">
        <EmptyState
          title="Coming soon"
          description="Support ticket management, SLA tracking, and escalation workflows will be available here."
          icon="&#x2709;"
        />
      </div>
    </div>
  );
}
