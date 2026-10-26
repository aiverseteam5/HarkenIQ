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
  centralCommand: "Central Command",
  siteManager: "Site Manager",
  harkenNode: "Harken Node",
  harkenNodes: "Harken Nodes",
  operationalAgent: "Operational Agent",
  operationalAgents: "Operational Agents",
  externalAgentRuntime: "External Agent Runtime",
  externalAgentRuntimes: "External Agent Runtimes",
} as const;

/** The customer-facing planes, as a browser tab names them. */
export const PLANES = {
  centralCommand: `${TERMS.product} ${TERMS.centralCommand}`,
  siteManager: `${TERMS.product} ${TERMS.siteManager}`,
} as const;

/** Names the product has retired, and what replaced them. */
export const RETIRED_TERMS: Readonly<Record<string, string>> = {
  "Cluster Manager": TERMS.centralCommand,
};
