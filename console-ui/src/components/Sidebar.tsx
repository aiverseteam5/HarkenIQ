import { Link } from "react-router-dom";
import { BrandLockup } from "./BrandMark";
import IconButton from "./IconButton";

/* ── Types ────────────────────────────────────────── */

export interface SidebarItem {
  key: string;
  label: string;
  icon?: string;
  badge?: number;
}

export interface SidebarSection {
  label: string;
  items: SidebarItem[];
}

interface Props {
  sections: SidebarSection[];
  activeItem: string;
  /** The URL an item opens. A34.4: navigation is a real link, so "open in
   *  new tab", "copy link" and a middle click all work. */
  hrefFor: (key: string) => string;
  user: { name: string; email: string; role: string };
  onLogout?: () => void;
}

/* ── Component ────────────────────────────────────── */

/** A35: dark chrome drawn from tokens. Hover and the current page are CSS
 *  states, so a keyboard user sees the same thing a pointer does, and focus
 *  takes the inverse ring the light-surface ring could not provide here. */
export default function Sidebar({ sections, activeItem, hrefFor, user, onLogout }: Props) {
  const initials = user.name
    .split(" ")
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();

  return (
    <nav className="sidebar" aria-label="Main navigation">
      <div className="sidebar__brand">
        <BrandLockup inverse />
      </div>

      {sections.map((section) => (
        <div key={section.label}>
          <div className="sidebar__section-label">{section.label}</div>
          {section.items.map((item) => (
            <Link
              key={item.key}
              to={hrefFor(item.key)}
              className="sidebar__item"
              aria-current={activeItem === item.key ? "page" : undefined}
            >
              {item.icon && (
                <span className="sidebar__item-icon" aria-hidden="true">
                  {item.icon}
                </span>
              )}
              <span>{item.label}</span>
              {item.badge !== undefined && item.badge > 0 && (
                <span className="sidebar__badge">{item.badge}</span>
              )}
            </Link>
          ))}
        </div>
      ))}

      <div className="sidebar__user">
        <div className="sidebar__avatar" aria-hidden="true">
          {initials}
        </div>
        <div className="sidebar__user-text">
          <div className="sidebar__user-name truncate">{user.name}</div>
          <div className="sidebar__user-role">{user.role.replace(/_/g, " ")}</div>
        </div>
        {onLogout && <IconButton icon="sign-out" label="Sign out" inverse onClick={onLogout} />}
      </div>
    </nav>
  );
}
