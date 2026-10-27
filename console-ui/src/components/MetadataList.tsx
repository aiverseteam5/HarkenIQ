import type { ReactNode } from "react";

/** A35.6: label/value pairs as a real description list (`dl`), so the pair
 *  is a pair to assistive technology too. It replaces `detailRow`,
 *  `detailLabel` and `detailValue`, which the checkpoint found declared in
 *  14 files, 12 of them byte for byte. */

export function MetadataList({ children, className }: { children: ReactNode; className?: string }) {
  return <dl className={className ? `metadata-list ${className}` : "metadata-list"}>{children}</dl>;
}

export function MetadataRow({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <div className="metadata-row">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}
