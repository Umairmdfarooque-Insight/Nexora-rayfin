// Table and relationship rows for the Inventory tab (T-304 FR-5): each
// warning links to its evidence query. Ported unchanged from rayfin-ui's
// src/components/InventoryPanels.tsx.
import {
  Badge,
  Body1,
  Body1Strong,
  Button,
  Caption1,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { useState } from "react";

import { formatDateTime } from "../lib/format.ts";
import {
  formatPercent,
  tableKey,
  tableWarnings,
  warningColumn,
  warningEvidence,
} from "../lib/inventory.ts";
import type { InventoryTable, RelationshipEvidence, TableProfile } from "../lib/types.ts";

const useStyles = makeStyles({
  list: { listStyleType: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", rowGap: "8px" },
  row: {
    border: `1px solid ${tokens.colorNeutralStroke2}`,
    borderRadius: tokens.borderRadiusMedium,
    padding: "8px 12px",
    display: "flex",
    flexDirection: "column",
    rowGap: "6px",
    minWidth: 0,
  },
  head: { display: "flex", alignItems: "center", columnGap: "10px", flexWrap: "wrap" },
  mono: { fontFamily: tokens.fontFamilyMonospace, overflowWrap: "anywhere" },
  label: { color: tokens.colorNeutralForeground3 },
  grid: { width: "100%", borderCollapse: "collapse", fontSize: tokens.fontSizeBase200 },
  cell: { textAlign: "left", padding: "3px 8px", borderBottom: `1px solid ${tokens.colorNeutralStroke2}` },
  warning: { display: "flex", flexDirection: "column", rowGap: "4px" },
  evidence: {
    backgroundColor: tokens.colorNeutralBackground3,
    borderRadius: tokens.borderRadiusMedium,
    padding: "6px 10px",
    display: "flex",
    flexDirection: "column",
    rowGap: "4px",
  },
  sql: { margin: 0, whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontFamily: tokens.fontFamilyMonospace, fontSize: tokens.fontSizeBase200 },
});

function statusColor(status: string): "success" | "warning" | "danger" {
  return status === "ok" ? "success" : status === "warning" ? "warning" : "danger";
}

function value(v: string | number | boolean | null | undefined): string {
  return v === null || v === undefined ? "" : String(v);
}

/** The queries and numbers behind one warning. */
function Evidence({ profile, column }: { profile: TableProfile; column: string | null }) {
  const styles = useStyles();
  const { queries, stats } = warningEvidence(profile, column);
  return (
    <div className={styles.evidence} aria-label="Evidence">
      {stats && (
        <Body1>
          <Body1Strong>{column}: </Body1Strong>
          rows {value(profile.row_count)}, nulls {value(stats.null_count)}, distinct{" "}
          {value(stats.distinct_count)}
          {stats.approximate ? " (approximate)" : ""}
          {stats.duplicate_count !== null ? `, values appearing more than once ${stats.duplicate_count}` : ""}
          {stats.min !== null ? `, min ${stats.min}, max ${stats.max}` : ""}
        </Body1>
      )}
      {!stats && <Body1>Rows: {value(profile.row_count)}</Body1>}
      <Caption1 className={styles.label}>Queries that ran ({formatDateTime(profile.evidence.profiled_at)}):</Caption1>
      {queries.map((query) => (
        <pre key={query} className={styles.sql}>
          {query}
        </pre>
      ))}
    </div>
  );
}

function Warning({ text, table }: { text: string; table: InventoryTable }) {
  const styles = useStyles();
  const [open, setOpen] = useState(false);
  const profile = table.profile;
  const column = warningColumn(text, table.columns);
  return (
    <li className={styles.warning}>
      <div className={styles.head}>
        <Badge color="warning">Warning</Badge>
        <Body1>{text}</Body1>
        {profile && profile.evidence.queries.length > 0 && (
          <Button size="small" appearance="transparent" aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? "Hide evidence" : "Show evidence"}
          </Button>
        )}
      </div>
      {open && profile && <Evidence profile={profile} column={column} />}
    </li>
  );
}

export function TableRow({ table, candidate }: { table: InventoryTable; candidate: boolean }) {
  const styles = useStyles();
  const [open, setOpen] = useState(false);
  const profile = table.profile;
  const warnings = tableWarnings(table);
  const key = tableKey(table);
  return (
    <li className={styles.row}>
      <div className={styles.head}>
        <Body1Strong className={styles.mono}>{key}</Body1Strong>
        {candidate && <Badge color="brand">Candidate</Badge>}
        {profile ? (
          <Badge color={statusColor(profile.status)}>
            {profile.status === "ok" ? "Profiled" : profile.status === "warning" ? "Profiled with warnings" : "Profile failed"}
          </Badge>
        ) : (
          <Badge color="informative">Not profiled</Badge>
        )}
        <Caption1 className={styles.label}>
          {table.columns.length} columns
          {profile?.row_count != null ? ` · ${profile.row_count} rows` : ""}
          {profile?.freshness?.column ? ` · latest ${profile.freshness.column}: ${profile.freshness.max_value ?? "n/a"}` : ""}
        </Caption1>
        <Button size="small" appearance="transparent" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? "Hide columns" : "Show columns"}
        </Button>
      </div>
      {warnings.length > 0 && (
        <ul className={styles.list} aria-label={`Warnings for ${key}`}>
          {warnings.map((text) => (
            <Warning key={text} text={text} table={table} />
          ))}
        </ul>
      )}
      {open && (
        <table className={styles.grid}>
          <thead>
            <tr>
              {["Column", "Type", "Null", "Nulls", "Distinct", "Key", "Unique", "Min", "Max"].map((h) => (
                <th key={h} className={styles.cell}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {table.columns.map((column) => {
              const stats = profile?.columns[column.name];
              return (
                <tr key={column.name}>
                  <td className={`${styles.cell} ${styles.mono}`}>{column.name}</td>
                  <td className={styles.cell}>{column.data_type ?? ""}</td>
                  <td className={styles.cell}>{column.nullable === null ? "" : column.nullable ? "yes" : "no"}</td>
                  <td className={styles.cell}>{value(stats?.null_count)}</td>
                  <td className={styles.cell}>
                    {value(stats?.distinct_count)}
                    {stats?.approximate ? "~" : ""}
                  </td>
                  <td className={styles.cell}>{stats?.is_key_candidate ? "yes" : ""}</td>
                  <td className={styles.cell}>{stats?.is_unique == null ? "" : stats.is_unique ? "yes" : "no"}</td>
                  <td className={styles.cell}>{value(stats?.min)}</td>
                  <td className={styles.cell}>{value(stats?.max)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </li>
  );
}

function confidenceColor(confidence: string | null): "success" | "warning" | "danger" | "informative" {
  return confidence === "high" ? "success" : confidence === "medium" ? "warning" : confidence === "low" ? "danger" : "informative";
}

export function RelationshipRow({ item }: { item: RelationshipEvidence }) {
  const styles = useStyles();
  const [open, setOpen] = useState(false);
  const rejected = item.status === "rejected";
  return (
    <li className={styles.row}>
      <div className={styles.head}>
        <Body1Strong>{item.id}</Body1Strong>
        <span className={styles.mono}>
          {item.from.table}.{item.from.column} → {item.to.table}.{item.to.column}
        </span>
        <Badge color={rejected ? "danger" : confidenceColor(item.confidence)}>
          {rejected ? "Rejected" : `${item.confidence ?? "n/a"} confidence`}
        </Badge>
        <Badge appearance="outline">{item.source}</Badge>
        {item.evidence.queries.length > 0 && (
          <Button size="small" appearance="transparent" aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? "Hide SQL" : "Show SQL"}
          </Button>
        )}
      </div>
      {rejected ? (
        <Body1>Reason: {item.rejected_reason}</Body1>
      ) : (
        <Caption1 className={styles.label}>
          {item.cardinality ?? "cardinality n/a"} · overlap {formatPercent(item.overlap_pct)} · orphans{" "}
          {formatPercent(item.orphan_rate)} · null keys {formatPercent(item.null_rate)} · types{" "}
          {item.type_compatible === null ? "n/a" : item.type_compatible ? "compatible" : "differ"}
        </Caption1>
      )}
      {item.notes.map((note) => (
        <Body1 key={note}>{note}</Body1>
      ))}
      {open && (
        <div className={styles.evidence} aria-label="Evidence">
          <Caption1 className={styles.label}>Queries that ran ({formatDateTime(item.evidence.computed_at)}):</Caption1>
          {item.evidence.queries.map((query) => (
            <pre key={query} className={styles.sql}>
              {query}
            </pre>
          ))}
        </div>
      )}
    </li>
  );
}
