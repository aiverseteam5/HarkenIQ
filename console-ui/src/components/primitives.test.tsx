// @vitest-environment jsdom
/** A34.8: the shared primitives meet the accessibility baseline.
 *
 *  Each fix lands in the component every page already uses, so these tests
 *  are about the component, not any one page.
 */

import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { act, screen } from "@testing-library/react";
import DataTable, { type Column } from "./DataTable";
import DataState from "./DataState";
import Toast from "./Toast";
import type { ToastItem } from "./useToast";
import { renderWithUser } from "../test/render";
import { axeViolations } from "../test/axe";
import type { DataStatus, Failure } from "../viewModel/dataState";

interface Row {
  id: string;
  name: string;
}

const rows: Row[] = [
  { id: "a", name: "Alpha" },
  { id: "b", name: "Bravo" },
];
const columns: Column<Row>[] = [{ key: "name", header: "Name" }];

describe("table rows that open something open it from the keyboard", () => {
  it("Enter and Space on a focused row open it", async () => {
    const onRowClick = vi.fn();
    const { user } = renderWithUser(<DataTable columns={columns} data={rows} onRowClick={onRowClick} />);
    await user.tab();
    expect(document.activeElement?.textContent).toBe("Alpha");
    await user.keyboard("{Enter}");
    await user.tab();
    await user.keyboard(" ");
    expect(onRowClick.mock.calls.map(([r]) => r.id)).toEqual(["a", "b"]);
  });

  it("a row that opens nothing is not a Tab stop", () => {
    const { container } = renderWithUser(<DataTable columns={columns} data={rows} />);
    expect(container.querySelectorAll("tr[tabindex]")).toHaveLength(0);
  });

  it("a button inside a row keeps its own meaning", async () => {
    const onRowClick = vi.fn();
    const onAction = vi.fn();
    const { user } = renderWithUser(
      <DataTable
        columns={columns}
        data={rows.slice(0, 1)}
        onRowClick={onRowClick}
        rowActions={[{ label: "Revoke", onClick: onAction }]}
      />,
    );
    screen.getByRole("button", { name: "Revoke" }).focus();
    await user.keyboard("{Enter}");
    expect(onAction).toHaveBeenCalledOnce();
    expect(onRowClick).not.toHaveBeenCalled();
  });

  it("has no axe violations", async () => {
    const { container } = renderWithUser(<DataTable columns={columns} data={rows} onRowClick={() => {}} />);
    expect(await axeViolations(container)).toEqual([]);
  });
});

describe("the toast live region exists before its first message", () => {
  function Toaster() {
    const [toasts, setToasts] = useState<ToastItem[]>([]);
    return (
      <>
        <button type="button" onClick={() => setToasts([{ id: 1, message: "Approved", variant: "success" }])}>
          ok
        </button>
        <button type="button" onClick={() => setToasts([{ id: 2, message: "Refused: scope", variant: "error" }])}>
          fail
        </button>
        <Toast toasts={toasts} onDismiss={() => setToasts([])} />
      </>
    );
  }

  it("is present and empty with nothing to say", () => {
    renderWithUser(<Toaster />);
    const region = screen.getByTestId("toast-region");
    expect(region.getAttribute("aria-live")).toBe("polite");
    expect(region.textContent).toBe("");
  });

  it("a success waits its turn; an error interrupts", async () => {
    const { user } = renderWithUser(<Toaster />);
    await user.click(screen.getByRole("button", { name: "ok" }));
    expect(screen.getByRole("status").textContent).toContain("Approved");
    await user.click(screen.getByRole("button", { name: "fail" }));
    expect(screen.getByRole("alert").textContent).toContain("Refused: scope");
  });
});

describe("one rendering for every data state", () => {
  const denied: Failure = { kind: "denied", status: 403, message: "requires fleet.view" };
  const down: Failure = { kind: "unavailable", status: 503, message: "no central_command registered" };

  const cases: [DataStatus, Partial<Parameters<typeof DataState>[0]>, RegExp][] = [
    ["loading", {}, /Loading this incident/],
    ["not_found", {}, /Not found, or not visible in your current scope/],
    ["denied", { failure: denied }, /Not available to you.*requires fleet\.view/s],
    ["refused", { failure: { kind: "refused", status: 422, message: "status: bad value" } }, /status: bad value/],
    ["unavailable", { failure: down }, /Unavailable.*no central_command registered/s],
  ];

  it.each(cases)("%s says so in words", (status, props, words) => {
    const { container } = renderWithUser(<DataState status={status} subject="this incident" {...props} />);
    expect(container.textContent).toMatch(words);
    expect(screen.getByRole("status")).toBeTruthy();
  });

  it("not found never claims which of absent or hidden it is", () => {
    const { container } = renderWithUser(<DataState status="not_found" subject="this incident" />);
    expect(container.textContent).toContain("cannot say which");
    expect(container.textContent).not.toMatch(/does not exist|deleted|no permission/i);
  });

  it("stale keeps the data on screen and says why", () => {
    const { container } = renderWithUser(
      <DataState status="stale" subject="the fleet" failure={down} lastSuccessAt={Date.now()}>
        <p>the rows</p>
      </DataState>,
    );
    expect(container.textContent).toContain("the rows");
    expect(container.textContent).toMatch(/latest refresh failed: no central_command registered/);
  });

  it("ready renders only what it wraps", () => {
    const { container } = renderWithUser(
      <DataState status="ready" subject="x">
        <p>content</p>
      </DataState>,
    );
    expect(container.textContent).toBe("content");
  });

  it.each(cases)("%s has no axe violations", async (status, props) => {
    const { container } = renderWithUser(<DataState status={status} subject="this incident" {...props} />);
    await act(async () => {});
    expect(await axeViolations(container)).toEqual([]);
  });
});
