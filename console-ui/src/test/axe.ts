/** A34.8: axe on the shared primitives and the shell.
 *
 *  A baseline, not an audit: jsdom has no layout and no colour, so contrast
 *  is not checked here (WCAG 2.2 AA evidence is PX8's), and a component
 *  rendered alone is not a page, so the "content inside a landmark" rule
 *  is left to tests that render the shell.
 */

import axe from "axe-core";

export async function axeViolations(
  node: Element,
  { page = false }: { page?: boolean } = {},
): Promise<string[]> {
  const results = await axe.run(node, {
    rules: {
      "color-contrast": { enabled: false },
      region: { enabled: page },
    },
  });
  return results.violations.map(
    (v) => `${v.id}: ${v.help} -- ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`,
  );
}
