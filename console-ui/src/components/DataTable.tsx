import { type ReactNode } from "react";
import Icon from "./Icon";
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
  /** Marks the row whose detail is open, so the reader keeps their place. */
  isRowSelected?: (row: T) => boolean;
  striped?: boolean;
}

/* ── Component ────────────────────────────────────── */

/** A35.6: the table primitive, at compact density.
 *
 *  A sortable header is a real button, so sorting works from the keyboard
 *  (the checkpoint found Fleet Overview's sort unreachable by keyboard).
 *  Hover, selection and focus are drawn by global.css from tokens; hover
 *  never carries information a keyboard user would miss. */
export default function DataTable<T extends object>({
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
  isRowSelected,
  striped,
}: Props<T>) {
  const totalPages = page !== undefined && pageSize && total !== undefined
    ? Math.max(1, Math.ceil(total / pageSize))
    : 0;

  const start = page !== undefined && pageSize ? (page - 1) * pageSize + 1 : 0;
  const end = page !== undefined && pageSize && total !== undefined
    ? Math.min(page * pageSize, total)
    : 0;

  const span = columns.length + (rowActions && rowActions.length > 0 ? 1 : 0);

  return (
    <div className={striped ? "data-table data-table--striped" : "data-table"}>
      <table>
        <thead>
          <tr>
            {columns.map((col) => {
              const sortable = !!col.sortKey && !!onSort;
              const isActive = sortable && sortColumn === col.sortKey;
              return (
                <th
                  key={col.key}
                  style={col.width !== undefined ? { width: col.width } : undefined}
                  data-align={col.align && col.align !== "left" ? col.align : undefined}
                  aria-sort={
                    isActive ? (sortDirection === "asc" ? "ascending" : "descending") : undefined
                  }
                >
                  {sortable ? (
                    <button type="button" className="data-table__sort" onClick={() => onSort(col.sortKey!)}>
                      {col.header}
                      <Icon
                        name={isActive ? (sortDirection === "asc" ? "chevron-up" : "chevron-down") : "sort-none"}
                        size={12}
                      />
                    </button>
                  ) : (
                    col.header
                  )}
                </th>
              );
            })}
            {rowActions && rowActions.length > 0 && (
              <th className="data-table__actions" style={{ width: "1%" }}>
                Actions
              </th>
            )}
          </tr>
        </thead>
        <tbody>
          {loading && (
            <tr>
              <td colSpan={span} className="data-table__message">
                <Spinner size="md" />
              </td>
            </tr>
          )}
          {!loading && data.length === 0 && (
            <tr>
              <td colSpan={span} className="data-table__message">
                {emptyIcon && (
                  <span className="data-table__message-icon" aria-hidden="true">
                    {emptyIcon}
                  </span>
                )}
                {emptyMessage}
              </td>
            </tr>
          )}
          {!loading &&
            data.map((row, ri) => (
              <tr
                key={ri}
                data-clickable={onRowClick ? "" : undefined}
                aria-current={isRowSelected?.(row) ? "true" : undefined}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                // A34.8: a row that opens something on click opens it from
                // the keyboard too. Only the row itself answers Enter and
                // Space, so a button inside it keeps its own meaning.
                tabIndex={onRowClick ? 0 : undefined}
                onKeyDown={
                  onRowClick
                    ? (e) => {
                        if (e.target !== e.currentTarget) return;
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          onRowClick(row);
                        }
                      }
                    : undefined
                }
              >
                {columns.map((col) => (
                  <td key={col.key} data-align={col.align && col.align !== "left" ? col.align : undefined}>
                    {col.render
                      ? col.render(row)
                      : String((row as Record<string, unknown>)[col.key] ?? "")}
                  </td>
                ))}
                {rowActions && rowActions.length > 0 && (
                  <td className="data-table__actions">
                    {rowActions.map((action, ai) => (
                      <button
                        key={ai}
                        type="button"
                        className={action.variant === "danger" ? "btn btn-sm btn-danger" : "btn btn-sm"}
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
        <div className="data-table__pagination">
          <span>
            Showing {total > 0 ? start : 0}–{end} of {total}
          </span>
          <div className="data-table__pagination-controls">
            <button
              type="button"
              className="btn btn-sm"
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
              type="button"
              className="btn btn-sm"
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
