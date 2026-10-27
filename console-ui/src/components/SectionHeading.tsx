import type { ReactNode } from "react";

/** A35.6: a section title that is a real heading, so a screen reader can
 *  move by it (WCAG 1.3.1). The checkpoint found 104 section titles drawn
 *  as styled divs. `level` follows the page's outline: the page title is
 *  the h1, so a section is an h2 and a section inside it an h3. */

interface Props {
  level?: 2 | 3;
  id?: string;
  actions?: ReactNode;
  children: ReactNode;
}

export default function SectionHeading({ level = 2, id, actions, children }: Props) {
  const Heading = level === 2 ? "h2" : "h3";
  return (
    <div className="section-heading">
      <Heading id={id} className="section-heading__title">
        {children}
      </Heading>
      {actions && <div className="section-heading__actions">{actions}</div>}
    </div>
  );
}
