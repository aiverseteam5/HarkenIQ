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
 * - A35: its colours are semantic tokens (global.css `.context-bar`).
 */

type Emphasis = "critical" | "warning";

/** Safety states that must be noticed are said in words AND colour
 *  (A28.18: status is never colour alone). */
const SAFETY_EMPHASIS: Partial<Record<SafetyFact["state"], Emphasis>> = {
  tenant_stop: "critical",
  site_halt: "critical",
  not_reported: "warning",
  partially_reported: "warning",
};

function ContextFact({
  label,
  fact,
  emphasis,
  live = false,
}: {
  label: string;
  fact: Fact<string>;
  emphasis?: Emphasis;
  live?: boolean;
}) {
  return (
    <div
      className="context-bar__fact"
      data-fact={label.toLowerCase()}
      data-state={fact.state}
      data-emphasis={emphasis}
    >
      <dt>{label}</dt>
      <dd title={fact.detail} aria-live={live ? "polite" : undefined}>
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
    <section aria-label="Operational context" className="context-bar">
      {isPlatform ? (
        <>
          {/* D4 (A35.2): the product is HarkenIQ Console; this names the plane. */}
          <span className="context-bar__crumb">Platform</span>
          <span className="context-bar__crumb" aria-hidden="true">›</span>
          <span className="context-bar__crumb">Tenants</span>
          <span className="context-bar__crumb" aria-hidden="true">›</span>
        </>
      ) : (
        <span className="context-bar__crumb">Tenant</span>
      )}
      <span className="context-bar__tenant" title={view.tenant.detail}>
        {view.tenant.text}
      </span>

      {/* A platform identity is not asked about (Central Command refuses
          it), so its four facts are all "not applicable" -- said once by
          leaving them out, not four times. */}
      {!isPlatform && (
        <dl className="context-bar__facts">
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
              ? "warning"
              : undefined}
          />
        </dl>
      )}

      {isPlatform && (
        <span className="context-bar__spacer">
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
