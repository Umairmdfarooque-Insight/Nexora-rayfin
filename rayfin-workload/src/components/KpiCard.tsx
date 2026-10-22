// A KPI's formula intent, grain, filters, time logic, exclusions, currency and
// examples (T-205 FR-2), with inline edit while the contract is a draft (FR-3).
import {
  Badge,
  Body1,
  Body1Strong,
  Button,
  Caption1,
  Field,
  Input,
  makeStyles,
  Subtitle2,
  tokens,
} from "@fluentui/react-components";
import { useState } from "react";

import { formatList, parseList } from "../lib/review.ts";
import type { Kpi, KpiChanges } from "../lib/types.ts";
import { LongText } from "./LongText.tsx";

const useStyles = makeStyles({
  card: {
    backgroundColor: tokens.colorNeutralBackground1,
    borderRadius: tokens.borderRadiusLarge,
    boxShadow: tokens.shadow4,
    padding: "12px 16px",
    display: "flex",
    flexDirection: "column",
    rowGap: "8px",
    minWidth: 0,
  },
  header: { display: "flex", alignItems: "center", columnGap: "12px", flexWrap: "wrap" },
  grow: { flexGrow: 1 },
  fields: { display: "grid", gridTemplateColumns: "160px 1fr", gap: "4px 12px" },
  label: { color: tokens.colorNeutralForeground3 },
  rationale: {
    backgroundColor: tokens.colorNeutralBackground3,
    borderRadius: tokens.borderRadiusMedium,
    padding: "8px 12px",
  },
  form: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px 16px" },
  wide: { gridColumn: "1 / -1" },
  actions: { display: "flex", columnGap: "8px" },
});

interface Props {
  kpi: Kpi;
  rationale?: string;
  /** Inline edit is offered only while the contract is a draft. */
  canEdit: boolean;
  busy: boolean;
  onSave: (name: string, changes: KpiChanges) => Promise<boolean>;
}

const NONE = "Not set";

export function KpiCard({ kpi, rationale, canEdit, busy, onSave }: Props) {
  const styles = useStyles();
  const [editing, setEditing] = useState(false);
  const [formula, setFormula] = useState("");
  const [grain, setGrain] = useState("");
  const [currency, setCurrency] = useState("");
  const [timeLogic, setTimeLogic] = useState("");
  const [filters, setFilters] = useState("");
  const [exclusions, setExclusions] = useState("");

  function start() {
    setFormula(kpi.formula_intent ?? "");
    setGrain(kpi.grain ?? "");
    setCurrency(kpi.currency ?? "");
    setTimeLogic(kpi.time_logic ?? "");
    setFilters(formatList(kpi.filters));
    setExclusions(formatList(kpi.exclusions));
    setEditing(true);
  }

  async function save() {
    // Only fields that changed are sent.
    const changes: KpiChanges = {};
    const text = (value: string) => (value.trim() === "" ? null : value.trim());
    if (text(formula) !== kpi.formula_intent) changes.formula_intent = text(formula);
    if (text(grain) !== kpi.grain) changes.grain = text(grain);
    if (text(currency) !== kpi.currency) changes.currency = text(currency);
    if (text(timeLogic) !== kpi.time_logic) changes.time_logic = text(timeLogic);
    if (formatList(parseList(filters)) !== formatList(kpi.filters)) changes.filters = parseList(filters);
    if (formatList(parseList(exclusions)) !== formatList(kpi.exclusions)) {
      changes.exclusions = parseList(exclusions);
    }
    if (Object.keys(changes).length === 0) {
      setEditing(false);
      return;
    }
    if (await onSave(kpi.name, changes)) {
      setEditing(false);
    }
  }

  const rows: [string, string | null][] = [
    ["Formula intent", kpi.formula_intent],
    ["Grain", kpi.grain],
    ["Filters", kpi.filters.length ? formatList(kpi.filters) : null],
    ["Time logic", kpi.time_logic],
    ["Exclusions", kpi.exclusions.length ? formatList(kpi.exclusions) : null],
    ["Currency", kpi.currency],
  ];

  return (
    <section className={styles.card} aria-label={`KPI ${kpi.name}`}>
      <div className={styles.header}>
        <Subtitle2 className={styles.grow}>{kpi.name}</Subtitle2>
        {kpi.status === "data_gap" && <Badge color="danger">Data gap</Badge>}
        {canEdit && !editing && (
          <Button size="small" onClick={start} disabled={busy}>
            Edit
          </Button>
        )}
      </div>

      {editing ? (
        <>
          <div className={styles.form}>
            <Field label="Formula intent" className={styles.wide}>
              <Input value={formula} onChange={(_, d) => setFormula(d.value)} />
            </Field>
            <Field label="Grain">
              <Input value={grain} onChange={(_, d) => setGrain(d.value)} />
            </Field>
            <Field label="Currency">
              <Input value={currency} onChange={(_, d) => setCurrency(d.value)} />
            </Field>
            <Field label="Time logic">
              <Input value={timeLogic} onChange={(_, d) => setTimeLogic(d.value)} />
            </Field>
            <Field label="Filters (comma-separated)">
              <Input value={filters} onChange={(_, d) => setFilters(d.value)} />
            </Field>
            <Field label="Exclusions (comma-separated)" className={styles.wide}>
              <Input value={exclusions} onChange={(_, d) => setExclusions(d.value)} />
            </Field>
          </div>
          <div className={styles.actions}>
            <Button appearance="primary" onClick={() => void save()} disabled={busy}>
              Save
            </Button>
            <Button onClick={() => setEditing(false)} disabled={busy}>
              Cancel
            </Button>
          </div>
        </>
      ) : (
        <div className={styles.fields}>
          {rows.map(([label, value]) => (
            <div key={label} style={{ display: "contents" }}>
              <Caption1 className={styles.label}>{label}</Caption1>
              <div>{value ? <LongText text={value} /> : <Body1>{NONE}</Body1>}</div>
            </div>
          ))}
          {kpi.expected_examples.length > 0 && (
            <>
              <Caption1 className={styles.label}>Examples</Caption1>
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {kpi.expected_examples.map((example, index) => (
                  <li key={index}>
                    {example.description}
                    {example.value !== null && example.value !== undefined ? `: ${String(example.value)}` : ""}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}

      {rationale && (
        <div className={styles.rationale}>
          <Body1Strong>Explanation</Body1Strong>
          <LongText text={rationale} />
        </div>
      )}
    </section>
  );
}
