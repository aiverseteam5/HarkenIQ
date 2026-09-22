/** A30.25 (A6-4B0b, D2): a site the caller sees as CONTEXT, not authority.
 *
 *  Central Command returns two kinds of row from `/sites`. A site the
 *  caller HOLDS comes back exactly as it always has. A site that merely
 *  CONTAINS a device the caller reads -- the caller is scoped to a device
 *  or a device class, not to the site -- comes back reduced to
 *  `{ id, site_name, contextual: true }`, so a row about that device can
 *  be placed somewhere.
 *
 *  WHY THIS MODULE EXISTS
 *  ----------------------
 *  Context is not authority, and the server enforces it: `covers_site()`
 *  is false for a contextual site and every site mutation refuses it.
 *  The Console must not OFFER what the server will refuse. A contextual
 *  row carries no `org_unit_id`, so on the Organization page it would
 *  otherwise read as an "unattached" site that can be attached, and in
 *  the Access Scope grant form it would appear as a site a grant can be
 *  made at. Both are authority surfaces; both use `authoritativeSites`.
 *
 *  Navigation filters (Fleet, Agents) keep contextual rows on purpose:
 *  naming the site a device is at is exactly what they are for.
 *
 *  Presentation only. Nothing here answers an authorization question.
 */

/** Any site row as it arrives over the wire. Only a contextual row
 *  carries the marker; an authoritative row has no such key. */
export interface MaybeContextualSite {
  contextual?: boolean;
}

/** True only for the explicit marker. A missing key, `false`, or any
 *  other value is an authoritative row -- the server never sends the
 *  marker for a site the caller holds. */
export function isContextualSite(site: MaybeContextualSite): boolean {
  return site.contextual === true;
}

/** The sites the caller holds: what an authority surface may offer. */
export function authoritativeSites<T extends MaybeContextualSite>(
  sites: readonly T[],
): T[] {
  return sites.filter((site) => !isContextualSite(site));
}
