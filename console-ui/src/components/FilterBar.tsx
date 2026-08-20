import { type CSSProperties } from "react";

/* ── Types ────────────────────────────────────────── */

export interface FilterDef {
  key: string;
  label: string;
  type: "text" | "select" | "dateRange";
  options?: { value: string; label: string }[];
  placeholder?: string;
}

interface Props {
  filters: FilterDef[];
  values: Record<string, string>;
  onChange: (key: string, value: string) => void;
  onClear: () => void;
}

/* ── Styles ───────────────────────────────────────── */

const barStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "0.5rem",
  flexWrap: "wrap",
  marginBottom: "1rem",
};

const searchWrap: CSSProperties = {
  position: "relative",
  display: "flex",
  alignItems: "center",
};

const searchIcon: CSSProperties = {
  position: "absolute",
  left: "0.625rem",
  color: "var(--text-muted)",
  fontSize: "0.8125rem",
  pointerEvents: "none",
};

/* ── Component ────────────────────────────────────── */

export default function FilterBar({ filters, values, onChange, onClear }: Props) {
  const hasValues = Object.values(values).some((v) => v !== "");

  return (
    <div style={barStyle}>
      {filters.map((f) => {
        if (f.type === "text") {
          return (
            <div key={f.key} style={searchWrap}>
              <span style={searchIcon} aria-hidden="true">&#x1F50D;</span>
              <input
                className="input"
                type="text"
                placeholder={f.placeholder ?? `Search ${f.label.toLowerCase()}...`}
                value={values[f.key] ?? ""}
                onChange={(e) => onChange(f.key, e.target.value)}
                aria-label={f.label}
                style={{ paddingLeft: "2rem", width: 220 }}
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
              style={{ width: "auto", minWidth: 140 }}
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
        if (f.type === "dateRange") {
          return (
            <input
              key={f.key}
              className="input"
              type="date"
              value={values[f.key] ?? ""}
              onChange={(e) => onChange(f.key, e.target.value)}
              aria-label={f.label}
              style={{ width: "auto" }}
            />
          );
        }
        return null;
      })}
      {hasValues && (
        <button
          className="btn btn-ghost"
          onClick={onClear}
          style={{ fontSize: "0.75rem", color: "var(--text-secondary)" }}
        >
          Clear all
        </button>
      )}
    </div>
  );
}
