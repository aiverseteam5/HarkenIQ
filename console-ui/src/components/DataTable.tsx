import { type CSSProperties, type ReactNode } from "react";
import Spinner from "./Spinner";

/* ── Types ────────────────────────────────────────── */

export interface Column<T> {
  key: string;
  header: string;
  render?: (row: T) => ReactNode;
  sortKey?: string;
  width?: string | number;
  align?: "left" | "center" | "right";
}

export interface RowAction<T> {
  label: string;
  onClick: (row: T) => void;
  variant?: "default" | "danger";
}

interface Props<T> {
  columns: Column<T>[];
  data: T[];
  loading?: boolean;
  emptyMessage?: string;
  emptyIcon?: string;
  sortColumn?: string;
  sortDirection?: "asc" | "desc";
  onSort?: (column: string) => void;
  page?: number;
  pageSize?: number;
  total?: number;
  onPageChange?: (page: number) => void;
  rowActions?: RowAction<T>[];
  onRowClick?: (row: T) => void;
  striped?: boolean;
}

/* ── Styles ───────────────────────────────────────── */

const tableWrap: CSSProperties = {
  background: "var(--bg-card)",
  border: "1px solid var(--border-color)",
  borderRadius: "var(--radius-lg)",
  overflow: "hidden",
  boxShadow: "var(--shadow-sm)",
};

const tableStyle: CSSProperties = {
  width: "100%",
  borderCollapse: "collapse",
  fontSize: "0.8125rem",
};

const thStyle: CSSProperties = {
  textAlign: "left",
  padding: "0.625rem 0.75rem",
  fontWeight: 600,
  fontSize: "0.75rem",
  color: "var(--text-secondary)",
  textTransform: "uppercase",
  letterSpacing: "0.04em",
  borderBottom: "1px solid var(--border-color)",
  background: "var(--bg-primary)",
  userSelect: "none",
  whiteSpace: "nowrap",
};

const tdStyle: CSSProperties = {
  padding: "0.5625rem 0.75rem",
  borderBottom: "1px solid var(--border-light)",
  color: "var(--text-primary)",
};

const paginationStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  padding: "0.5rem 0.75rem",
  borderTop: "1px solid var(--border-color)",
  fontSize: "0.75rem",
  color: "var(--text-secondary)",
  background: "var(--bg-primary)",
};

/* ── Component ────────────────────────────────────── */

export default function DataTable<T extends Record<string, unknown>>({
  columns,
  data,
  loading,
  emptyMessage = "No data",
  emptyIcon,
  sortColumn,
  sortDirection,
  onSort,
  page,
  pageSize,
  total,
  onPageChange,
  rowActions,
  onRowClick,
  striped,
}: Props<T>) {
  const totalPages = page !== undefined && pageSize && total !== undefined
    ? Math.max(1, Math.ceil(total / pageSize))
    : 0;

  const start = page !== undefined && pageSize ? (page - 1) * pageSize + 1 : 0;
  const end = page !== undefined && pageSize && total !== undefined
    ? Math.min(page * pageSize, total)
    : 0;

  return (
    <div style={tableWrap}>
      <table style={tableStyle}>
        <thead>
          <tr>
            {columns.map((col) => {
              const sortable = !!col.sortKey && !!onSort;
              const isActive = sortColumn === col.sortKey;
              return (
                <th
                  key={col.key}
                  style={{
                    ...thStyle,
                    width: col.width,
                    textAlign: col.align ?? "left",
                    cursor: sortable ? "pointer" : "default",
                  }}
                  onClick={sortable ? () => onSort(col.sortKey!) : undefined}
                  aria-sort={
                    isActive
                      ? sortDirection === "asc" ? "ascending" : "descending"
                      : undefined
                  }
                >
                  {col.header}
                  {isActive && (
                    <span style={{ marginLeft: "0.25rem" }}>
                      {sortDirection === "asc" ? "\u25B2" : "\u25BC"}
                    </span>
                  )}
                </th>
              );
            })}
            {rowActions && rowActions.length > 0 && (
              <th style={{ ...thStyle, width: "1%", textAlign: "right" }}>Actions</th>
            )}
          </tr>
        </thead>
        <tbody>
          {loading && (
            <tr>
              <td
                colSpan={columns.length + (rowActions ? 1 : 0)}
                style={{ ...tdStyle, textAlign: "center", padding: "2rem" }}
              >
                <Spinner size="md" />
              </td>
            </tr>
          )}
          {!loading && data.length === 0 && (
            <tr>
              <td
                colSpan={columns.length + (rowActions ? 1 : 0)}
                style={{ ...tdStyle, textAlign: "center", padding: "2rem", color: "var(--text-muted)" }}
              >
                {emptyIcon && <span style={{ display: "block", fontSize: "1.5rem", marginBottom: "0.375rem" }}>{emptyIcon}</span>}
                {emptyMessage}
              </td>
            </tr>
          )}
          {!loading &&
            data.map((row, ri) => (
              <tr
                key={ri}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                style={{
                  cursor: onRowClick ? "pointer" : "default",
                  background: striped && ri % 2 === 1 ? "var(--bg-primary)" : undefined,
                  transition: "background var(--transition)",
                }}
                onMouseEnter={(e) => {
                  if (onRowClick) e.currentTarget.style.background = "var(--status-info-bg)";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background =
                    striped && ri % 2 === 1 ? "var(--bg-primary)" : "";
                }}
              >
                {columns.map((col) => (
                  <td key={col.key} style={{ ...tdStyle, textAlign: col.align ?? "left" }}>
                    {col.render
                      ? col.render(row)
                      : String(row[col.key] ?? "")}
                  </td>
                ))}
                {rowActions && rowActions.length > 0 && (
                  <td style={{ ...tdStyle, textAlign: "right", whiteSpace: "nowrap" }}>
                    {rowActions.map((action, ai) => (
                      <button
                        key={ai}
                        className={`btn ${action.variant === "danger" ? "btn-danger" : ""}`}
                        style={{ padding: "0.25rem 0.5rem", fontSize: "0.75rem", marginLeft: ai > 0 ? "0.375rem" : 0 }}
                        onClick={(e) => {
                          e.stopPropagation();
                          action.onClick(row);
                        }}
                      >
                        {action.label}
                      </button>
                    ))}
                  </td>
                )}
              </tr>
            ))}
        </tbody>
      </table>

      {page !== undefined && pageSize && total !== undefined && onPageChange && (
        <div style={paginationStyle}>
          <span>
            Showing {total > 0 ? start : 0}–{end} of {total}
          </span>
          <div style={{ display: "flex", gap: "0.375rem", alignItems: "center" }}>
            <button
              className="btn"
              style={{ padding: "0.25rem 0.5rem", fontSize: "0.75rem" }}
              disabled={page <= 1}
              onClick={() => onPageChange(page - 1)}
              aria-label="Previous page"
            >
              Prev
            </button>
            <span>
              Page {page} of {totalPages}
            </span>
            <button
              className="btn"
              style={{ padding: "0.25rem 0.5rem", fontSize: "0.75rem" }}
              disabled={page >= totalPages}
              onClick={() => onPageChange(page + 1)}
              aria-label="Next page"
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
