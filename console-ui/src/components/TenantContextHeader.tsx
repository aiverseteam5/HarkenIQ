import { type CSSProperties } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../useAuth";
import { useOperationalContext } from "../context/OperationalContext";
import type { Fact, SafetyFact } from "../viewModel/operationalContext";

/**
 * Says which tenant you are inside, gives platform staff a way out, and --
 * A34.6 -- states the operational context: effective scope, enforcement
 * posture, safety state and freshness.
 *
 * This is the context model's first consumer, not the A28.11 context bar:
 * the facts are plain text in the bar that already existed, with no new
 * visual design (PX2 designs the bar). Every word comes from
 * `viewModel/operationalContext`; this component decides nothing.
 *
 * Review fixes (2026-08-28), still true:
 * - It renders in the pathless layout ABOVE the Outlet, where useParams
 *   returns {} -- so the tenant comes from the path, via the provider.
 * - The tenant NAME comes from /api/me/tenants for tenant users and from
 *   the platform registry only for platform staff.
 * - Tokens below are the real ones from variables.css.
 */

const barStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  flexWrap: "wrap",
  gap: "0.5rem",
  padding: "0.5rem 0.75rem",
  marginBottom: "1rem",
  borderRadius: "var(--radius-sm)",
  background: "var(--bg-card)",
  border: "1px solid var(--border-light)",
  fontSize: "0.8125rem",
};

const crumbMuted: CSSProperties = {
  color: "var(--text-secondary)",
};

const tenantNameStyle: CSSProperties = {
  fontWeight: 600,
  color: "var(--text-primary)",
};

const factsStyle: CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: "0.25rem 1rem",
  marginLeft: "1rem",
};

const factStyle: CSSProperties = { display: "flex", gap: "0.375rem" };

const spacerStyle: CSSProperties = { marginLeft: "auto" };

/** Safety states that must be noticed are said in words AND colour
 *  (A28.18: status is never colour alone). */
const SAFETY_EMPHASIS: Partial<Record<SafetyFact["state"], CSSProperties>> = {
  tenant_stop: { color: "var(--status-critical)", fontWeight: 700 },
  site_halt: { color: "var(--status-critical)", fontWeight: 700 },
  not_reported: { color: "var(--status-warning)", fontWeight: 600 },
  partially_reported: { color: "var(--status-warning)", fontWeight: 600 },
};

function ContextFact({
  label,
  fact,
  emphasis,
  live = false,
}: {
  label: string;
  fact: Fact<string>;
  emphasis?: CSSProperties;
  live?: boolean;
}) {
  return (
    <div style={factStyle} data-fact={label.toLowerCase()} data-state={fact.state}>
      <dt style={crumbMuted}>{label}</dt>
      <dd title={fact.detail} style={emphasis} aria-live={live ? "polite" : undefined}>
        {fact.text}
      </dd>
    </div>
  );
}

export default function TenantContextHeader() {
  const context = useOperationalContext();
  const { user } = useAuth();
  const navigate = useNavigate();

  if (!context) return null;

  const isPlatform = user?.is_platform_user ?? false;
  const { view } = context;

  return (
    <section aria-label="Operational context" style={barStyle}>
      {isPlatform ? (
        <>
          <span style={crumbMuted}>Platform Console</span>
          <span style={crumbMuted} aria-hidden="true">›</span>
          <span style={crumbMuted}>Tenants</span>
          <span style={crumbMuted} aria-hidden="true">›</span>
        </>
      ) : (
        <span style={crumbMuted}>Tenant</span>
      )}
      <span style={tenantNameStyle} title={view.tenant.detail}>
        {view.tenant.text}
      </span>

      {/* A platform identity is not asked about (Central Command refuses
          it), so its four facts are all "not applicable" -- said once by
          leaving them out, not four times. */}
      {!isPlatform && (
        <dl style={factsStyle}>
          <ContextFact label="Scope" fact={view.scope} />
          <ContextFact label="Enforcement" fact={view.posture} />
          <ContextFact
            label="Safety"
            fact={view.safety}
            emphasis={SAFETY_EMPHASIS[view.safety.state as SafetyFact["state"]]}
            live
          />
          <ContextFact
            label="Data"
            fact={view.freshness}
            emphasis={view.freshness.state === "stale" || view.freshness.state === "never"
              ? { color: "var(--status-warning)", fontWeight: 600 }
              : undefined}
          />
        </dl>
      )}

      {isPlatform && (
        <span style={spacerStyle}>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => navigate("/tenants")}
          >
            Switch tenant
          </button>
        </span>
      )}
    </section>
  );
}
