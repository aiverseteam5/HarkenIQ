// @vitest-environment jsdom
/** A35.6–A35.8: the primitives PX1B adds or normalizes, as a person and a
 *  screen reader meet them. axe runs on every one (A35.7); contrast is the
 *  contrast contract's job, because jsdom has no colour. */

import { describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import StatusBadge from "./StatusBadge";
import Icon from "./Icon";
import IconButton from "./IconButton";
import Banner from "./Banner";
import Panel from "./Panel";
import SectionHeading from "./SectionHeading";
import { MetadataList, MetadataRow } from "./MetadataList";
import { BrandLockup, BrandMark } from "./BrandMark";
import PageHeader from "./PageHeader";
import DataTable, { type Column } from "./DataTable";
import MetricCard from "./MetricCard";
import { renderWithUser } from "../test/render";
import { axeViolations } from "../test/axe";
import { TONES, statusTone } from "../viewModel/status";

describe("StatusBadge: a status said in words, its tone in colour AND shape", () => {
  it.each(TONES)("%s renders its words, its tone and its own glyph", (tone) => {
    const { container } = renderWithUser(<StatusBadge status={`state ${tone}`} tone={tone} />);
    const badge = container.querySelector(".status-badge")!;
    expect(badge.textContent).toBe(`state ${tone}`);
    expect(badge.getAttribute("data-tone")).toBe(tone);
    const glyph = badge.querySelector("svg")!;
    expect(glyph.getAttribute("aria-hidden")).toBe("true");
    expect(glyph.getAttribute("data-icon")).toBe(`tone-${tone}`);
  });

  it("every tone's glyph is a different shape (A35.5 rule 7)", () => {
    const shapes = TONES.map((tone) => {
      const { container, unmount } = renderWithUser(<Icon name={`tone-${tone}`} />);
      const html = container.querySelector("svg")!.innerHTML;
      unmount();
      return html;
    });
    expect(new Set(shapes).size).toBe(TONES.length);
  });

  it("has no axe violations at either size", async () => {
    const { container } = renderWithUser(
      <div>
        {TONES.map((tone) => (
          <StatusBadge key={tone} status={tone} tone={tone} size="sm" />
        ))}
        <StatusBadge status="approved" tone={statusTone("approval.decision", "approved")} />
      </div>,
    );
    expect(await axeViolations(container)).toEqual([]);
  });
});

describe("IconButton: a glyph control with a name", () => {
  it("is announced by its label and works from the keyboard", async () => {
    const onClick = vi.fn();
    const { user } = renderWithUser(<IconButton icon="close" label="Close panel" onClick={onClick} />);
    const button = screen.getByRole("button", { name: "Close panel" });
    expect(button.querySelector("svg")?.getAttribute("aria-hidden")).toBe("true");
    await user.tab();
    expect(document.activeElement).toBe(button);
    await user.keyboard("{Enter}");
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("has no axe violations, plain or inverse", async () => {
    const { container } = renderWithUser(
      <div>
        <IconButton icon="close" label="Dismiss" />
        <IconButton icon="sign-out" label="Sign out" inverse />
      </div>,
    );
    expect(await axeViolations(container)).toEqual([]);
  });
});

describe("Banner, Panel, SectionHeading, MetadataList: structure a screen reader can use", () => {
  it("SectionHeading is a real heading at the level asked for", () => {
    renderWithUser(
      <>
        <SectionHeading>Device info</SectionHeading>
        <SectionHeading level={3}>Firmware</SectionHeading>
      </>,
    );
    expect(screen.getByRole("heading", { level: 2, name: "Device info" })).toBeTruthy();
    expect(screen.getByRole("heading", { level: 3, name: "Firmware" })).toBeTruthy();
  });

  it("a titled Panel is a region named by its heading", () => {
    renderWithUser(<Panel title="Warranty">covered</Panel>);
    const region = screen.getByRole("region", { name: "Warranty" });
    expect(within(region).getByRole("heading", { name: "Warranty" })).toBeTruthy();
  });

  it("MetadataList pairs each label with its value in a dl", () => {
    const { container } = renderWithUser(
      <MetadataList>
        <MetadataRow label="Vendor">Dell</MetadataRow>
        <MetadataRow label="Model">R750</MetadataRow>
      </MetadataList>,
    );
    const dl = container.querySelector("dl")!;
    expect([...dl.querySelectorAll("dt")].map((d) => d.textContent)).toEqual(["Vendor", "Model"]);
    expect([...dl.querySelectorAll("dd")].map((d) => d.textContent)).toEqual(["Dell", "R750"]);
  });

  it("a Banner carries its tone as a shape and its message as words", () => {
    const { container } = renderWithUser(
      <Banner tone="stale" title="Out of date" role="status">
        Showing data as of 14:02.
      </Banner>,
    );
    const banner = screen.getByRole("status");
    expect(banner.getAttribute("data-tone")).toBe("stale");
    expect(banner.textContent).toContain("Out of date");
    expect(container.querySelector("[data-icon='tone-stale']")).toBeTruthy();
  });

  it("has no axe violations together", async () => {
    const { container } = renderWithUser(
      <main>
        <h1>Page</h1>
        <Panel title="Details">
          <SectionHeading level={3}>Info</SectionHeading>
          <MetadataList>
            <MetadataRow label="Site">London</MetadataRow>
          </MetadataList>
          <Banner tone="warning">Check this.</Banner>
        </Panel>
      </main>,
    );
    expect(await axeViolations(container)).toEqual([]);
  });
});

describe("BrandMark and the HarkenIQ Console lockup (A35.8, D4)", () => {
  it("the words are the name; the mark is decoration and never smaller than 16px", () => {
    const { container } = renderWithUser(<BrandLockup />);
    expect(container.textContent).toBe("HarkenIQConsole");
    const mark = container.querySelector("[data-brand-mark]")!;
    expect(mark.getAttribute("aria-hidden")).toBe("true");
    const tiny = renderWithUser(<BrandMark size={8} />).container.querySelector("svg")!;
    expect(Number(tiny.getAttribute("width"))).toBeGreaterThanOrEqual(16);
  });
});

describe("PageHeader breadcrumbs (A35.6)", () => {
  function header() {
    return renderWithUser(
      <MemoryRouter>
        <PageHeader title="Invoice 42" breadcrumbs={[{ label: "HarkenIQ" }, { label: "Billing", href: "/billing" }, { label: "Invoice 42" }]} />
      </MemoryRouter>,
    );
  }

  it("is an ordered list whose last item is the current page", () => {
    const { container } = header();
    const nav = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(nav).getAllByRole("listitem")).toHaveLength(3);
    expect(container.querySelector("[aria-current='page']")?.textContent).toBe("Invoice 42");
  });

  it("separators are not read aloud, and an internal link is a router link", () => {
    const { container } = header();
    for (const sep of container.querySelectorAll(".breadcrumbs__separator")) {
      expect(sep.getAttribute("aria-hidden")).toBe("true");
    }
    expect(screen.getByRole("link", { name: "Billing" }).getAttribute("href")).toBe("/billing");
  });

  it("has no axe violations", async () => {
    const { container } = header();
    expect(await axeViolations(container)).toEqual([]);
  });
});

describe("DataTable sorting works from the keyboard (A35.6)", () => {
  interface Row {
    id: string;
    name: string;
  }
  const columns: Column<Row>[] = [{ key: "name", header: "Name", sortKey: "name" }];
  const rows: Row[] = [{ id: "a", name: "Alpha" }];

  it("a sortable header is a button that Enter activates", async () => {
    const onSort = vi.fn();
    const { user } = renderWithUser(
      <DataTable columns={columns} data={rows} onSort={onSort} sortColumn="name" sortDirection="asc" />,
    );
    const sort = screen.getByRole("button", { name: /Name/ });
    await user.tab();
    expect(document.activeElement).toBe(sort);
    await user.keyboard("{Enter}");
    expect(onSort).toHaveBeenCalledWith("name");
    expect(screen.getByRole("columnheader").getAttribute("aria-sort")).toBe("ascending");
  });

  it("the open row is marked current, not selected-by-colour alone", () => {
    const { container } = renderWithUser(
      <DataTable columns={columns} data={rows} onRowClick={() => {}} isRowSelected={(r) => r.id === "a"} />,
    );
    expect(container.querySelector("tbody tr")?.getAttribute("aria-current")).toBe("true");
  });

  it("has no axe violations with sorting on", async () => {
    const { container } = renderWithUser(<DataTable columns={columns} data={rows} onSort={() => {}} sortColumn="name" />);
    expect(await axeViolations(container)).toEqual([]);
  });
});

describe("MetricCard shows a direction, never a verdict (A35.5)", () => {
  it("names the direction in words and colours neither way", () => {
    const { container } = renderWithUser(<MetricCard title="Open Incidents" value={3} trend="down" trendValue="2" />);
    const trend = container.querySelector(".metric-card__trend")!;
    expect(trend.textContent).toContain("down");
    expect(trend.getAttribute("data-tone")).toBeNull();
  });
});
