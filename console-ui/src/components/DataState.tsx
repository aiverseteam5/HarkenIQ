import { type CSSProperties, type ReactNode } from "react";
import Spinner from "./Spinner";
import type { DataStatus, Failure } from "../viewModel/dataState";

/** A34.8: one accessible rendering for every state a read can be in.
 *
 *  The words matter more than the layout, so they live here once:
 *
 *  - NOT FOUND never claims which of "absent" or "not visible to you" it
 *    is. Central Command answers both with the same 404 on purpose, and a
 *    screen that guessed would leak what the server withheld.
 *  - DENIED and REFUSED show the server's own reason (P4).
 *  - STALE keeps the data on screen and says how old it is (P7: silence
 *    never renders as health).
 *
 *  Every state is announced politely; nothing here interrupts.
 */

interface Props {
  status: DataStatus;
  /** What is being loaded, as it reads mid-sentence: "this incident". */
  subject: string;
  failure?: Failure | null;
  /** Epoch ms of the last success, for stale and unavailable. */
  lastSuccessAt?: number | null;
  /** For partial: the parts that did not load. */
  missing?: readonly string[];
  /** Rendered for ready, stale and partial. */
  children?: ReactNode;
}

const blockStyle: CSSProperties = {
  padding: "1.5rem 1rem",
  textAlign: "center",
  color: "var(--text-secondary)",
  fontSize: "0.875rem",
  lineHeight: 1.5,
};

const titleStyle: CSSProperties = {
  display: "block",
  color: "var(--text-primary)",
  fontWeight: 600,
  marginBottom: "0.25rem",
};

const noticeStyle: CSSProperties = {
  padding: "0.5rem 0.75rem",
  marginBottom: "0.75rem",
  borderRadius: "var(--radius-sm)",
  background: "var(--status-warning-bg)",
  color: "var(--text-primary)",
  fontSize: "0.8125rem",
};

function capitalize(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

function at(ms: number | null | undefined): string {
  return ms ? new Date(ms).toLocaleTimeString() : "";
}

function reason(failure: Failure | null | undefined): string {
  if (!failure) return "";
  if (failure.message) return failure.message;
  return failure.status ? `HTTP ${failure.status}` : "no response";
}

export default function DataState({
  status,
  subject,
  failure,
  lastSuccessAt,
  missing = [],
  children,
}: Props) {
  switch (status) {
    case "ready":
      return <>{children}</>;

    case "loading":
      return (
        <div role="status" aria-busy="true" style={blockStyle}>
          <Spinner size="md" label={null} />
          <span style={{ display: "block", marginTop: "0.5rem" }}>Loading {subject}…</span>
        </div>
      );

    case "stale":
      return (
        <>
          <div role="status" style={noticeStyle}>
            Showing {subject} as of {at(lastSuccessAt) || "an earlier refresh"}.{" "}
            {failure ? `The latest refresh failed: ${reason(failure)}.` : "It has not refreshed since."}
          </div>
          {children}
        </>
      );

    case "partial":
      return (
        <>
          <div role="status" style={noticeStyle}>
            Part of {subject} could not be loaded
            {missing.length ? `: ${missing.join(", ")}` : ""}. What is shown is complete for
            the parts that did load.
          </div>
          {children}
        </>
      );

    case "not_found":
      return (
        <div role="status" style={blockStyle}>
          <strong style={titleStyle}>Not found, or not visible in your current scope</strong>
          {capitalize(subject)} was not returned. Central Command gives the same answer for
          both, so this page cannot say which.
        </div>
      );

    case "denied":
      return (
        <div role="status" style={blockStyle}>
          <strong style={titleStyle}>Not available to you</strong>
          The server refused to show {subject} to your identity
          {failure?.message ? `: ${failure.message}` : "."}
        </div>
      );

    case "refused":
      return (
        <div role="status" style={blockStyle}>
          <strong style={titleStyle}>Request refused</strong>
          {reason(failure) || "The server refused this request."}
        </div>
      );

    case "unavailable":
      return (
        <div role="status" style={blockStyle}>
          <strong style={titleStyle}>Unavailable</strong>
          {capitalize(subject)} could not be loaded ({reason(failure)}).
          {lastSuccessAt ? ` Last loaded at ${at(lastSuccessAt)}.` : ""}
        </div>
      );
  }
}
