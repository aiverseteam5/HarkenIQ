// @vitest-environment jsdom
/** A34.8: overlays own the keyboard while open (A28.18).
 *
 *  The COMPONENT test pattern (A34.3): render, drive by keyboard, check with
 *  axe. The scenario is the one that was broken: a confirmation opened from
 *  inside a drawer (Users -> Disable User), where one Escape used to close
 *  both, and neither ever held or returned focus.
 */

import { useState } from "react";
import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import DetailPanel from "./DetailPanel";
import ConfirmDialog from "./ConfirmDialog";
import { renderWithUser } from "../test/render";
import { axeViolations } from "../test/axe";

function DrawerWithConfirm() {
  const [panel, setPanel] = useState(false);
  const [confirm, setConfirm] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setPanel(true)}>
        Open user
      </button>
      <DetailPanel open={panel} onClose={() => setPanel(false)} title="ops@example.com">
        <button type="button" onClick={() => setConfirm(true)}>
          Disable user
        </button>
        <a href="#audit">Audit trail</a>
      </DetailPanel>
      <ConfirmDialog
        open={confirm}
        title="Disable user?"
        message="They lose access immediately."
        variant="danger"
        confirmLabel="Disable"
        onConfirm={() => setConfirm(false)}
        onCancel={() => setConfirm(false)}
      />
    </>
  );
}

describe("a drawer holds focus while open and gives it back", () => {
  it("focus moves into the drawer, which is named by its title", async () => {
    const { user } = renderWithUser(<DrawerWithConfirm />);
    await user.click(screen.getByRole("button", { name: "Open user" }));
    const drawer = screen.getByRole("dialog", { name: "ops@example.com" });
    expect(document.activeElement).toBe(drawer);
  });

  it("Tab and Shift+Tab cycle inside the drawer", async () => {
    const { user } = renderWithUser(<DrawerWithConfirm />);
    await user.click(screen.getByRole("button", { name: "Open user" }));
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Close panel" }));
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Disable user" }));
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("link", { name: "Audit trail" }));
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Close panel" }));
    await user.tab({ shift: true });
    expect(document.activeElement).toBe(screen.getByRole("link", { name: "Audit trail" }));
  });

  it("Escape closes it and focus returns to what opened it", async () => {
    const { user } = renderWithUser(<DrawerWithConfirm />);
    const opener = screen.getByRole("button", { name: "Open user" });
    await user.click(opener);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(opener);
  });
});

describe("a confirmation over a drawer", () => {
  async function openBoth() {
    const r = renderWithUser(<DrawerWithConfirm />);
    await r.user.click(screen.getByRole("button", { name: "Open user" }));
    await r.user.click(screen.getByRole("button", { name: "Disable user" }));
    return r;
  }

  it("starts on Cancel, the choice that changes nothing", async () => {
    await openBoth();
    expect(screen.getByRole("dialog", { name: "Disable user?" })).toHaveProperty(
      "ariaModal",
      "true",
    );
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Cancel" }));
  });

  it("is described by its message", async () => {
    await openBoth();
    const dialog = screen.getByRole("dialog", { name: "Disable user?" });
    const described = document.getElementById(dialog.getAttribute("aria-describedby") ?? "");
    expect(described?.textContent).toBe("They lose access immediately.");
  });

  it("one Escape closes only the confirmation; the drawer stays, and focus returns to its button", async () => {
    const { user } = await openBoth();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Disable user?" })).toBeNull();
    expect(screen.getByRole("dialog", { name: "ops@example.com" })).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Disable user" }));
  });

  it("Tab cannot leave the confirmation for the drawer behind it", async () => {
    const { user } = await openBoth();
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Disable" }));
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Cancel" }));
  });

  it("has no axe violations, stacked", async () => {
    const { container } = await openBoth();
    expect(await axeViolations(container)).toEqual([]);
  });
});

describe("a confirmation that asks for input", () => {
  it("starts in its first field instead", () => {
    renderWithUser(
      <ConfirmDialog open title="Reopen ticket?" onConfirm={() => {}} onCancel={() => {}}>
        <label htmlFor="why">Reason</label>
        <input id="why" />
      </ConfirmDialog>,
    );
    expect(document.activeElement).toBe(screen.getByLabelText("Reason"));
  });
});

describe("two confirmations on one page", () => {
  it("each is labelled by its own title -- ids are per instance", () => {
    renderWithUser(
      <>
        <ConfirmDialog open title="Delete role?" message="a" onConfirm={() => {}} onCancel={() => {}} />
        <ConfirmDialog open title="Disable user?" message="b" onConfirm={() => {}} onCancel={() => {}} />
      </>,
    );
    expect(screen.getByRole("dialog", { name: "Delete role?" })).toBeTruthy();
    expect(screen.getByRole("dialog", { name: "Disable user?" })).toBeTruthy();
  });
});
