import Icon from "./Icon";

/* ── Types ────────────────────────────────────────── */

export interface FilterDef {
  key: string;
  label: string;
  type: "text" | "select" | "date" | "dateRange";
  options?: { value: string; label: string }[];
  placeholder?: string;
}

interface Props {
  filters: FilterDef[];
  values: Record<string, string>;
  onChange: (key: string, value: string) => void;
  onClear: () => void;
}

/* ── Component ────────────────────────────────────── */

export default function FilterBar({ filters, values, onChange, onClear }: Props) {
  const hasValues = Object.values(values).some((v) => v !== "");

  return (
    <div className="filter-bar">
      {filters.map((f) => {
        if (f.type === "text") {
          return (
            <div key={f.key} className="filter-bar__search">
              <Icon name="search" size={14} className="filter-bar__search-icon" />
              <input
                className="input"
                type="text"
                placeholder={f.placeholder ?? `Search ${f.label.toLowerCase()}...`}
                value={values[f.key] ?? ""}
                onChange={(e) => onChange(f.key, e.target.value)}
                aria-label={f.label}
              />
            </div>
          );
        }
        if (f.type === "select" && f.options) {
          return (
            <select
              key={f.key}
              className="select"
              value={values[f.key] ?? ""}
              onChange={(e) => onChange(f.key, e.target.value)}
              aria-label={f.label}
            >
              <option value="">{f.label}: All</option>
              {f.options.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          );
        }
        if (f.type === "date" || f.type === "dateRange") {
          return (
            <input
              key={f.key}
              className="input"
              type="date"
              value={values[f.key] ?? ""}
              onChange={(e) => onChange(f.key, e.target.value)}
              aria-label={f.label}
            />
          );
        }
        return null;
      })}
      {hasValues && (
        <button type="button" className="btn btn-ghost btn-sm" onClick={onClear}>
          Clear all
        </button>
      )}
    </div>
  );
}
