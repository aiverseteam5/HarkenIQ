/** A34.7: the names HarkenIQ uses for itself and its actors (A28.7).
 *
 *  Three different things are "agents" in HarkenIQ, and a screen that says
 *  "agent" leaves the reader to guess which -- the per-device software that
 *  is the final execution authority, the governed actor a tenant configures,
 *  or somebody's own software holding that actor's credential. So the bare
 *  word is never a label: `terminology.test.ts` refuses it in every
 *  user-facing string position, and these are the words that replace it.
 *
 *  A device is the hardware; a Harken Node is HarkenIQ running on it. The
 *  two are not interchangeable either.
 */

export const TERMS = {
  product: "HarkenIQ",
  console: "Console",
  centralCommand: "Central Command",
  siteManager: "Site Manager",
  harkenNode: "Harken Node",
  harkenNodes: "Harken Nodes",
  operationalAgent: "Operational Agent",
  operationalAgents: "Operational Agents",
  externalAgentRuntime: "External Agent Runtime",
  externalAgentRuntimes: "External Agent Runtimes",
} as const;

/** D4 (A35.2): the product a person signs in to is HarkenIQ Console, on
 *  both planes; its tab says so, and the context bar says which plane.
 *  Central Command is the control plane it talks to, the Site Manager the
 *  site-local runtime and control plane, a Harken Node the execution
 *  runtime. Text still names Central Command where it means that system. */
export const PLANES = {
  console: `${TERMS.product} ${TERMS.console}`,
  siteManager: `${TERMS.product} ${TERMS.siteManager}`,
} as const;

/** Names the product has retired, and what replaced them. */
export const RETIRED_TERMS: Readonly<Record<string, string>> = {
  "Cluster Manager": TERMS.centralCommand,
  "Platform Console": PLANES.console,
};
