"use client";

import {
  Badge,
  Body1,
  Body1Strong,
  Button,
  Caption1,
  Field,
  Input,
  makeStyles,
  MessageBar,
  MessageBarActions,
  MessageBarBody,
  MessageBarTitle,
  Spinner,
  Subtitle2,
  Textarea,
  Title2,
  tokens,
} from "@fluentui/react-components";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ErrorBanner } from "@/components/ErrorBanner";
import { RelationshipRow, TableRow } from "@/components/InventoryPanels";
import { LongText } from "@/components/LongText";
import { StateBadge } from "@/components/StateBadge";
import { api, type ApiError } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import {
  acceptBlockedReason,
  candidateTables,
  canAcceptInventory,
  exceptionRecords,
  filterTables,
  hasDataRole,
  INVENTORY_STALE_MESSAGE,
  inventoryHash,
  missingExceptions,
  paginate,
  parseSchemas,
  tableKey,
} from "@/lib/inventory";
import { isStale } from "@/lib/review";
import type {
  ApprovalRecord,
  GateResult,
  MetadataInventory,
  Me,
  RunDetail,
} from "@/lib/types";

const DATA_STATES = ["REQUIREMENT_APPROVED", "DATA_PROFILED"];

const useStyles = makeStyles({
  page: { display: "flex", flexDirection: "column", rowGap: "12px" },
  titleRow: { display: "flex", alignItems: "center", columnGap: "12px", flexWrap: "wrap" },
  mono: { fontFamily: tokens.fontFamilyMonospace, color: tokens.colorNeutralForeground3, overflowWrap: "anywhere" },
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
  columns: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: "16px", alignItems: "start" },
  stack: { display: "flex", flexDirection: "column", rowGap: "12px", minWidth: 0 },
  checks: { listStyleType: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", rowGap: "6px" },
  check: { display: "flex", columnGap: "8px", alignItems: "baseline", flexWrap: "wrap" },
  actions: { display: "flex", columnGap: "8px", alignItems: "center", flexWrap: "wrap" },
  list: { listStyleType: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", rowGap: "8px" },
  label: { color: tokens.colorNeutralForeground3 },
  pager: { display: "flex", columnGap: "8px", alignItems: "center" },
});

export default function InventoryPage() {
  const styles = useStyles();
  const { id } = useParams<{ id: string }>();
  const [run, setRun] = useState<RunDetail | null>(null);
  const [me, setMe] = useState<Me | null>(null);
  const [inventory, setInventory] = useState<MetadataInventory | null>(null);
  const [gate, setGate] = useState<GateResult | null>(null);
  const [approvals, setApprovals] = useState<ApprovalRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<{ title: string; error: ApiError } | null>(null);
  const [stale, setStale] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [narrow, setNarrow] = useState("");
  const [gap, setGap] = useState("");
  const [rationales, setRationales] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setStale(false);
    try {
      const [loaded, who, records] = await Promise.all([
        api.getRun(id),
        api.getMe(),
        api.listApprovals(id),
      ]);
      setRun(loaded);
      setMe(who);
      setApprovals(records);
      if (inventoryHash(loaded)) {
        const [inv, result] = await Promise.all([api.getInventory(id), api.checkG2(id)]);
        setInventory(inv);
        setGate(result);
      } else {
        setInventory(null);
        setGate(null);
      }
    } catch (e) {
      setError({ title: "Couldn't load the inventory.", error: e as ApiError });
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  /** Runs one API call, then reloads. Returns true when the API accepted it. */
  async function act(label: string, call: () => Promise<unknown>): Promise<boolean> {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      await call();
      await load();
      return true;
    } catch (e) {
      const failure = e as ApiError;
      if (isStale(failure)) {
        setStale(true);
      } else {
        setError({ title: `Couldn't ${label}.`, error: failure });
      }
      return false;
    } finally {
      setBusy(null);
    }
  }

  if (error?.error.status === 404 && !run) {
    return (
      <div className={styles.page}>
        <MessageBar intent="warning" role="alert">
          <MessageBarBody>Run not found: {id}</MessageBarBody>
        </MessageBar>
        <Link href="/runs">Back to runs</Link>
      </div>
    );
  }

  const working = busy !== null || loading;
  const canChange = !!run && DATA_STATES.includes(run.state);
  const dataEngineer = hasDataRole(me);
  const candidates = new Set(candidateTables(gate));
  const missing = missingExceptions(gate);
  const exceptions = exceptionRecords(approvals);
  const blocked = acceptBlockedReason(run, gate, me);
  const filtered = inventory ? filterTables(inventory.tables, query) : [];
  const paged = paginate(filtered, page);
  const columnCount = inventory?.tables.reduce((n, t) => n + t.columns.length, 0) ?? 0;

  return (
    <div className={styles.page}>
      <div className={styles.titleRow}>
        <Title2 as="h1">Inventory</Title2>
        <Link href={`/runs/${encodeURIComponent(id)}`}>Back to run</Link>
        <Button size="small" onClick={() => void load()} disabled={working}>
          Reload
        </Button>
        {working && <Spinner size="tiny" />}
      </div>

      {stale && (
        <MessageBar intent="warning" role="alert">
          <MessageBarBody>
            <MessageBarTitle>Out of date.</MessageBarTitle>
            {INVENTORY_STALE_MESSAGE} Nothing was changed.
          </MessageBarBody>
          <MessageBarActions>
            <Button onClick={() => void load()} disabled={loading}>
              Reload
            </Button>
          </MessageBarActions>
        </MessageBar>
      )}
      {error && (
        <ErrorBanner
          title={error.title}
          error={error.error}
          onRetry={error.error.unreachable ? () => void load() : undefined}
          disabled={working}
        />
      )}
      {notice && (
        <MessageBar intent="success">
          <MessageBarBody>{notice}</MessageBarBody>
        </MessageBar>
      )}
      {loading && !run && <Spinner label="Loading inventory…" />}

      {run && (
        <section className={styles.card} aria-label="Summary">
          <div className={styles.titleRow}>
            <StateBadge state={run.state} />
            {inventory && (
              <>
                <Badge color={run.artifacts.MetadataInventory?.status === "approved" ? "success" : "informative"}>
                  {run.artifacts.MetadataInventory?.status === "approved" ? "Accepted" : "Draft"}
                </Badge>
                <Caption1 className={styles.label}>
                  Version {inventory.version} · harvested {formatDateTime(inventory.harvested_at)}
                </Caption1>
                <span className={styles.mono}>{inventoryHash(run)}</span>
              </>
            )}
          </div>
          {inventory ? (
            <Body1>
              {inventory.tables.length} tables, {columnCount} columns, {inventory.errors.length} errors, scope{" "}
              {inventory.scope.schemas.join(", ") || "all schemas"}.
            </Body1>
          ) : (
            <Body1>No metadata has been harvested for this run yet.</Body1>
          )}
          {inventory?.warnings.map((warning) => (
            <MessageBar key={warning} intent="warning">
              <MessageBarBody>{warning}</MessageBarBody>
            </MessageBar>
          ))}
          {inventory?.errors.map((e) => (
            <MessageBar key={`${e.table}-${e.code}`} intent="warning">
              <MessageBarBody>
                {e.table}: {e.code}, {e.message}
              </MessageBarBody>
            </MessageBar>
          ))}
          {run.data_gaps.length > 0 && (
            <MessageBar intent="warning">
              <MessageBarBody>
                <MessageBarTitle>Data gap raised.</MessageBarTitle>
                {run.data_gaps[run.data_gaps.length - 1].description}
              </MessageBarBody>
            </MessageBar>
          )}
        </section>
      )}

      {run && (
        <div className={styles.columns}>
          <div className={styles.stack}>
            <section className={styles.card} aria-label="Prepare the inventory">
              <Subtitle2>Prepare the inventory</Subtitle2>
              {!canChange && <Body1>A run in {run.state} can't change its inventory.</Body1>}
              <div className={styles.actions}>
                <Button
                  disabled={working || !canChange}
                  onClick={() => void act("harvest the metadata", () => api.harvest(id))}
                >
                  {inventory ? "Re-harvest" : "Harvest"}
                </Button>
                <Button
                  disabled={working || !canChange || !inventory}
                  onClick={() =>
                    void act("profile the candidate tables", async () => {
                      const counts = await api.profile(id);
                      setNotice(`Profiled ${counts.profiled} tables (${counts.warnings} warnings, ${counts.errors} errors).`);
                    })
                  }
                >
                  Profile candidates
                </Button>
                <Button
                  disabled={working || !canChange || !inventory}
                  onClick={() =>
                    void act("analyze the relationships", async () => {
                      const counts = await api.analyzeRelationships(id);
                      setNotice(`Analyzed ${counts.candidates} relationships (${counts.rejected} rejected).`);
                    })
                  }
                >
                  Analyze relationships
                </Button>
              </div>
              <Field label="Narrow the scope" hint="Schemas to harvest instead, separated by commas.">
                <Input
                  value={narrow}
                  disabled={working || !canChange}
                  placeholder="saleslt"
                  onChange={(_, data) => setNarrow(data.value)}
                />
              </Field>
              <div className={styles.actions}>
                <Button
                  disabled={working || !canChange || parseSchemas(narrow).length === 0}
                  onClick={() => void act("narrow the scope", () => api.harvest(id, parseSchemas(narrow)))}
                >
                  Re-harvest these schemas
                </Button>
              </div>
            </section>

            <section className={styles.card} aria-label="Raise a data gap">
              <Subtitle2>Raise a data gap</Subtitle2>
              <Body1>
                If the data this requirement needs isn't in the Lakehouse, raise a data gap. The run waits for input.
              </Body1>
              <Field label="What is missing?">
                <Textarea
                  value={gap}
                  resize="vertical"
                  maxLength={2000}
                  disabled={working || !canChange}
                  onChange={(_, data) => setGap(data.value)}
                />
              </Field>
              <div className={styles.actions}>
                <Button
                  disabled={working || !canChange || !gap.trim()}
                  onClick={async () => {
                    if (await act("raise the data gap", () => api.raiseDataGap(id, gap.trim()))) setGap("");
                  }}
                >
                  Raise data gap
                </Button>
              </div>
            </section>
          </div>

          <div className={styles.stack}>
            <section className={styles.card} aria-label="Gate G2">
              <Subtitle2>Gate G2 checklist</Subtitle2>
              {gate ? (
                <ul className={styles.checks}>
                  {gate.checks.map((check) => (
                    <li key={check.id} className={styles.check}>
                      <Badge color={check.status === "pass" ? "success" : check.status === "fail" ? "danger" : "warning"}>
                        {check.status === "pass" ? "Pass" : check.status === "fail" ? "Fail" : "Warning"}
                      </Badge>
                      <Body1Strong>{check.id}</Body1Strong>
                      <Body1>{check.description}</Body1>
                      {Object.keys(check.evidence).length > 0 && check.status !== "pass" && (
                        <Caption1 className={styles.label}>
                          <LongText text={JSON.stringify(check.evidence)} />
                        </Caption1>
                      )}
                    </li>
                  ))}
                </ul>
              ) : (
                <Body1>Harvest the metadata first.</Body1>
              )}
              <div className={styles.actions}>
                <Button
                  appearance="primary"
                  disabled={working || !canAcceptInventory(run, gate, me)}
                  onClick={() => {
                    const hash = inventoryHash(run);
                    if (!hash) return;
                    void act("accept the inventory", () =>
                      api.approve(id, { artifact: "MetadataInventory", action: "ACCEPT", expected_hash: hash }),
                    );
                  }}
                >
                  Accept
                </Button>
                {blocked && <Caption1 className={styles.label}>Accept is unavailable: {blocked}</Caption1>}
              </div>
            </section>

            <section className={styles.card} aria-label="Exceptions">
              <Subtitle2>Source exceptions</Subtitle2>
              {missing.length === 0 && <Body1>Every candidate table is conformed or has an exception.</Body1>}
              {missing.map((table) => (
                <div key={table} className={styles.stack}>
                  <Body1Strong>{table} is not a conformed source</Body1Strong>
                  {dataEngineer && run.state === "REQUIREMENT_APPROVED" ? (
                    <>
                      <Field label="Rationale">
                        <Textarea
                          value={rationales[table] ?? ""}
                          resize="vertical"
                          maxLength={2000}
                          disabled={working}
                          onChange={(_, data) => setRationales({ ...rationales, [table]: data.value })}
                        />
                      </Field>
                      <div className={styles.actions}>
                        <Button
                          disabled={working || !(rationales[table] ?? "").trim()}
                          onClick={() =>
                            void act("approve the exception", () =>
                              api.approveException(id, table, (rationales[table] ?? "").trim()),
                            )
                          }
                        >
                          Approve exception
                        </Button>
                      </div>
                    </>
                  ) : (
                    <Caption1 className={styles.label}>A data_engineer must approve an exception.</Caption1>
                  )}
                </div>
              ))}
              {exceptions.length > 0 && <Body1Strong>Approved exceptions</Body1Strong>}
              <ul className={styles.list}>
                {exceptions.map((record) => (
                  <li key={record.approval_id}>
                    <Body1>
                      {record.artifact.replace("Exception:", "")}: {record.comment}
                    </Body1>
                    <div>
                      <Caption1 className={styles.label}>
                        {record.user.upn} · {formatDateTime(record.at)}
                      </Caption1>
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          </div>
        </div>
      )}

      {inventory && (
        <section className={styles.card} aria-label="Tables">
          <Subtitle2>Tables</Subtitle2>
          <div className={styles.actions}>
            <Field label="Search tables">
              <Input
                value={query}
                placeholder="schema.table"
                onChange={(_, data) => {
                  setQuery(data.value);
                  setPage(1);
                }}
              />
            </Field>
            <Caption1 className={styles.label}>
              {filtered.length} of {inventory.tables.length} tables
            </Caption1>
          </div>
          {paged.items.length === 0 && <Body1>No tables match.</Body1>}
          <ul className={styles.list}>
            {paged.items.map((table) => (
              <TableRow key={tableKey(table)} table={table} candidate={candidates.has(tableKey(table))} />
            ))}
          </ul>
          {paged.pages > 1 && (
            <div className={styles.pager}>
              <Button size="small" disabled={paged.page <= 1} onClick={() => setPage(paged.page - 1)}>
                Previous
              </Button>
              <Caption1>
                Page {paged.page} of {paged.pages}
              </Caption1>
              <Button size="small" disabled={paged.page >= paged.pages} onClick={() => setPage(paged.page + 1)}>
                Next
              </Button>
            </div>
          )}
        </section>
      )}

      {inventory && (
        <section className={styles.card} aria-label="Relationships">
          <Subtitle2>Relationship evidence</Subtitle2>
          {inventory.relationship_evidence.length === 0 ? (
            <Body1>No relationships analyzed yet. Use Analyze relationships.</Body1>
          ) : (
            <ul className={styles.list}>
              {inventory.relationship_evidence.map((item) => (
                <RelationshipRow key={item.id} item={item} />
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  );
}
