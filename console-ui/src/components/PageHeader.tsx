import { type ReactNode } from "react";
import { Link } from "react-router-dom";

interface Breadcrumb {
  label: string;
  href?: string;
}

interface Props {
  title: string;
  breadcrumbs?: Breadcrumb[];
  actions?: ReactNode | HeaderAction[];
}

export interface HeaderAction {
  label: string;
  onClick: () => void | Promise<void> | undefined;
  variant?: "primary" | "default" | "danger";
}

function isActionArray(value: ReactNode | HeaderAction[]): value is HeaderAction[] {
  return Array.isArray(value) && value.every(
    (item) => item !== null && typeof item === "object" && "label" in item && "onClick" in item,
  );
}

const ACTION_CLASS: Record<string, string> = {
  primary: "btn btn-primary",
  danger: "btn btn-danger",
  default: "btn",
};

/** A35.6: the page's title and its trail.
 *
 *  The trail is an ordered list inside a labelled nav, the current page is
 *  marked `aria-current`, and the separators are hidden from screen readers
 *  (they read "slash" otherwise). An internal link is a router link, so
 *  following a crumb no longer reloads the whole Console. */
export default function PageHeader({ title, breadcrumbs, actions }: Props) {
  const renderedActions = isActionArray(actions) ? (
    <>
      {actions.map((action) => (
        <button
          key={action.label}
          type="button"
          className={ACTION_CLASS[action.variant ?? "default"] ?? "btn"}
          onClick={() => void action.onClick()}
        >
          {action.label}
        </button>
      ))}
    </>
  ) : (
    actions
  );
  return (
    <div className="page-header">
      {breadcrumbs && breadcrumbs.length > 0 && (
        <nav aria-label="Breadcrumb" className="breadcrumbs">
          <ol>
            {breadcrumbs.map((bc, i) => {
              const last = i === breadcrumbs.length - 1;
              return (
                <li key={i}>
                  {i > 0 && (
                    <span className="breadcrumbs__separator" aria-hidden="true">
                      /
                    </span>
                  )}
                  {bc.href && !last ? (
                    <Link to={bc.href}>{bc.label}</Link>
                  ) : (
                    <span aria-current={last ? "page" : undefined}>{bc.label}</span>
                  )}
                </li>
              );
            })}
          </ol>
        </nav>
      )}
      <div className="page-header__row">
        <h1 className="page-header__title">{title}</h1>
        {renderedActions && <div className="page-header__actions">{renderedActions}</div>}
      </div>
    </div>
  );
}
