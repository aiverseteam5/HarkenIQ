export interface CoverageEntry {
  device_id: string;
  agent_id: string;
  agent_name: string;
  observation: "observed" | "stale" | "unobserved";
  health: string;
  last_heartbeat_at: string | null;
  state: string;
}

export interface Device extends CoverageEntry {
  vendor: string;
  model: string;
  service_tag: string;
  rack_suggestion: string | null;
  first_seen_at: string;
}

export interface Incident {
  id: string;
  kind: string;
  status: string;
  parent_id: string | null;
  agent_id: string | null;
  domain_id: string | null;
  subsystem: string | null;
  confidence: number;
  inferred: boolean;
  title: string;
  correlation_meta: Record<string, unknown> | null;
  opened_at: string;
  resolved_at: string | null;
  children: Incident[];
}

export interface FaultDomain {
  id: string;
  name: string;
  kind: string;
  status: "inferred" | "confirmed";
  confidence: number;
  source: string;
  confirmed_by: string | null;
  members: string[];
}

export interface Action {
  id: string;
  agent_id: string | null;
  agent_action_id: string;
  type: string;
  sensor_id: string;
  skill_name: string;
  verdict_severity: string;
  params: Record<string, unknown> | null;
  status: string;
  proposed_at: string;
  decision: string | null;
  decided_by: string | null;
  decided_at: string | null;
  outcome: Record<string, unknown> | null;
  incident_id: string | null;
}
