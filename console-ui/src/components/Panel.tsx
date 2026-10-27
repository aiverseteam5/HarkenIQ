import { type ReactNode, useId } from "react";

/** A35.6: a raised surface that holds one part of a page. With a title it
 *  is a labelled region whose heading a screen reader can reach; without
 *  one it is only a surface. */

interface Props {
  title?: string;
  /** The heading level of the title within the page's outline. */
  level?: 2 | 3;
  actions?: ReactNode;
  className?: string;
  children: ReactNode;
}

export default function Panel({ title, level = 2, actions, className, children }: Props) {
  const titleId = useId();
  const Heading = level === 2 ? "h2" : "h3";
  return (
    <section className={className ? `panel ${className}` : "panel"} aria-labelledby={title ? titleId : undefined}>
      {title && (
        <div className="panel__header">
          <Heading id={titleId} className="section-heading__title">
            {title}
          </Heading>
          {actions && <div className="section-heading__actions">{actions}</div>}
        </div>
      )}
      <div className="panel__body">{children}</div>
    </section>
  );
}
