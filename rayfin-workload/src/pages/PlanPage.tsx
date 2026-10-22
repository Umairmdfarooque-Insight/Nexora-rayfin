// Plan review (T-405 FR-4): prepare the plan (resolve sources, check
// feasibility, generate -- jobs, via api.resolveSources/checkFeasibility/
// generatePlan), the Gate G3 checklist, storage mode, tables with their
// rationale and rejected alternatives, relationships with evidence and
// Remove/Override, measures with Remove, data-engineering actions and the
// approval actions. Ported from rayfin-ui's src/app/runs/[id]/plan/page.tsx
// to this workload's pages (T-104).
import {
  Badge,
  Body1,
  Body1Strong,
  Button,
  Caption1,
  Dropdown,
  Field,
  Input,
  makeStyles,
  MessageBar,
  MessageBarActions,
  MessageBarBody,
  MessageBarTitle,
  Option,
  Spinner,
  Subtitle2,
  Textarea,
  Title2,
  tokens,
} from "@fluentui/react-components";
import { useCallback, useEffect, useState } from "react";

import { ErrorBanner } from "../components/ErrorBanner.tsx";
import { JobProgress } from "../components/JobProgress.tsx";
import { StateBadge } from "../components/StateBadge.tsx";
import { type ApiError, api } from "../lib/api.ts";
import { formatDateTime } from "../lib/format.ts";
import {
  acceptBlockedReason,
  blockingActions,
  canEditPlan,
  canOverride,
  canRejectPlan,
  checkDetails,
  evidenceFor,
  evidenceSummary,
  overrideRelationship,
  PLAN_STALE_MESSAGE,
  planApprovals,
  planHash,
  rejectedTables,
  removeMeasure,
  removeRelationship,
  setStorageMode,
  tableRationale,
  unresolvedEntities,
} from "../lib/plan.ts";
import { isStale } from "../lib/review.ts";
import type {
  ApprovalRecord,
  GateResult,
  Me,
  MetadataInventory,
  PlanOp,
  PlanResult,
  RunDetail,
  SourceResolution,
} from "../lib/types.ts";

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
  list: { listStyleType: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", rowGap: "10px" },
  item: { display: "flex", flexDirection: "column", rowGap: "4px", minWidth: 0 },
  label: { color: tokens.colorNeutralForeground3 },
  detail: { color: tokens.colorNeutralForeground3, overflowWrap: "anywhere" },
  queries: { fontFamily: tokens.fontFamilyMonospace, fontSize: tokens.fontSizeBase200, whiteSpace: "pre-wrap" },
});

export function PlanPage({ runId }: { runId: string }) {
  const styles = useStyles();
  const [run, setRun] = useState<RunDetail | null>(null);
  const [me, setMe] = useState<Me | null>(null);
  const [plan, setPlan] = useState<PlanResult | null>(null);
  const [inventory, setInventory] = useState<MetadataInventory | null>(null);
  const [resolution, setResolution] = useState<SourceResolution | null>(null);
  const [gate, setGate] = useState<GateResult | null>(null);
  const [approvals, setApprovals] = useState<ApprovalRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<{ title: string; error: ApiError } | null>(null);
  const [stale, setStale] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [comment, setComment] = useState("");
  const [rationales, setRationales] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setStale(false);
    try {
      const [loaded, who, records] = await Promise.all([api.getRun(runId), api.getMe(), api.listApprovals(runId)]);
      setRun(loaded);
      setMe(who);
      setApprovals(records);
      if (planHash(loaded)) {
        const [saved, inv, result] = await Promise.all([api.getPlan(runId), api.getInventory(runId), api.checkG3(runId)]);
        setPlan(saved);
        setInventory(inv);
        setGate(result);
        setResolution(await api.getSourceResolution(runId).catch(() => null));
      } else {
        setPlan(null);
        setGate(null);
      }
    } catch (e) {
      setError({ title: "Couldn't load the plan.", error: e as ApiError });
    } finally {
      setLoading(false);
    }
  }, [runId]);

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

  function edit(label: string, ops: PlanOp[]) {
    const hash = planHash(run);
    if (!hash) return;
    void act(label, () => api.editPlan(runId, hash, ops));
  }

  if (error?.error.status === 404 && !run) {
    return (
      <div className={styles.page}>
        <MessageBar intent="warning" role="alert">
          <MessageBarBody>Run not found: {runId}</MessageBarBody>
        </MessageBar>
      </div>
    );
  }

  const jobRunning = run?.active_job?.status === "queued" || run?.active_job?.status === "running";
  const working = busy !== null || loading || jobRunning;
  const editable = canEditPlan(run);
  const blocked = acceptBlockedReason(run, gate, me);
  const model = plan?.model_spec ?? null;
  const execution = plan?.execution_plan ?? null;
  const rejected = rejectedTables(resolution);
  const unresolved = unresolvedEntities(resolution);
  const history = planApprovals(approvals);
  const status = run?.artifacts.ExecutionPlan?.status;

  return (
    <div className={styles.page}>
      <div className={styles.titleRow}>
        <Title2 as="h2">Plan review</Title2>
        <Button size="small" onClick={() => void load()} disabled={working}>
          Reload
        </Button>
        {working && <Spinner size="tiny" />}
      </div>

      {run?.active_job && <JobProgress job={run.active_job} />}

      {stale && (
        <MessageBar intent="warning" role="alert">
          <MessageBarBody>
            <MessageBarTitle>Out of date.</MessageBarTitle>
            {PLAN_STALE_MESSAGE} Nothing was changed.
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
      {loading && !run && <Spinner label="Loading plan…" />}

      {run && (
        <section className={styles.card} aria-label="Summary">
          <div className={styles.titleRow}>
            <StateBadge state={run.state} />
            {plan && model && execution && (
              <>
                <Badge color={status === "approved" ? "success" : "informative"}>
                  {status === "approved" ? "Approved" : "Draft"}
                </Badge>
                <Caption1 className={styles.label}>
                  Version {execution.version} · {model.model_name} · {model.storage_mode}
                </Caption1>
                <span className={styles.mono}>{planHash(run)}</span>
              </>
            )}
          </div>
          {plan && model ? (
            <Body1>
              {model.tables.length} tables, {model.relationships.length} relationships, {model.measures.length}{" "}
              measures, {model.roles.length} RLS roles. Target: {execution?.target.kind}{" "}
              {execution?.target.workspace_id ?? "(no workspace set)"}.
            </Body1>
          ) : (
            <Body1>No plan has been generated for this run yet.</Body1>
          )}
        </section>
      )}

      {run && (
        <div className={styles.columns}>
          <div className={styles.stack}>
            <section className={styles.card} aria-label="Prepare the plan">
              <Subtitle2>Prepare the plan</Subtitle2>
              {run.state !== "DATA_PROFILED" && <Body1>A run in {run.state} can't change its plan.</Body1>}
              <Body1>Resolve the sources, check each KPI, then generate the plan. Generating again replaces your edits.</Body1>
              <div className={styles.actions}>
                <Button
                  disabled={working || run.state !== "DATA_PROFILED"}
                  onClick={() => void act("resolve the sources", () => api.resolveSources(runId))}
                >
                  Resolve sources
                </Button>
                <Button
                  disabled={working || run.state !== "DATA_PROFILED"}
                  onClick={() => void act("check the KPIs", () => api.checkFeasibility(runId))}
                >
                  Check feasibility
                </Button>
                <Button
                  disabled={working || run.state !== "DATA_PROFILED"}
                  onClick={() => void act("generate the plan", () => api.generatePlan(runId))}
                >
                  {plan ? "Regenerate plan" : "Generate plan"}
                </Button>
              </div>
              {run.data_gaps.length > 0 && (
                <MessageBar intent="warning">
                  <MessageBarBody>
                    <MessageBarTitle>Data gap.</MessageBarTitle>
                    {run.data_gaps[run.data_gaps.length - 1].description}
                  </MessageBarBody>
                </MessageBar>
              )}
            </section>

            {model && (
              <section className={styles.card} aria-label="Storage mode">
                <Subtitle2>Storage mode</Subtitle2>
                <div className={styles.actions}>
                  <Dropdown
                    aria-label="Storage mode"
                    value={model.storage_mode}
                    selectedOptions={[model.storage_mode]}
                    disabled={working || !editable}
                    onOptionSelect={(_, data) => {
                      if ((data.optionValue === "DirectLake" || data.optionValue === "Import") && data.optionValue !== model.storage_mode) {
                        edit("change the storage mode", [setStorageMode(data.optionValue)]);
                      }
                    }}
                  >
                    <Option value="DirectLake">DirectLake</Option>
                    <Option value="Import">Import</Option>
                  </Dropdown>
                </div>
                <Caption1 className={styles.label}>Direct Lake can't use calculated tables.</Caption1>
              </section>
            )}
          </div>

          <div className={styles.stack}>
            <section className={styles.card} aria-label="Gate G3">
              <Subtitle2>Gate G3 checklist</Subtitle2>
              {gate ? (
                <ul className={styles.checks}>
                  {gate.checks.map((check) => (
                    <li key={check.id} className={styles.check}>
                      <Badge color={check.status === "pass" ? "success" : check.status === "fail" ? "danger" : "warning"}>
                        {check.status === "pass" ? "Pass" : check.status === "fail" ? "Fail" : "Warning"}
                      </Badge>
                      <Body1Strong>{check.id}</Body1Strong>
                      <Body1>{check.description}</Body1>
                      {check.status !== "pass" &&
                        checkDetails(check).map((line) => (
                          <Caption1 key={line} className={styles.detail}>
                            {line}
                          </Caption1>
                        ))}
                    </li>
                  ))}
                </ul>
              ) : (
                <Body1>Generate the plan first.</Body1>
              )}
              <Field label="Comment" hint="Needed to reject; optional to accept.">
                <Textarea
                  value={comment}
                  resize="vertical"
                  maxLength={2000}
                  disabled={working || !plan}
                  onChange={(_, data) => setComment(data.value)}
                />
              </Field>
              <div className={styles.actions}>
                <Button appearance="primary" disabled={working || blocked !== null} onClick={() => {
                  const hash = planHash(run);
                  if (!hash) return;
                  void act("accept the plan", () =>
                    api.approve(runId, { artifact: "ExecutionPlan", action: "ACCEPT", expected_hash: hash, comment: comment.trim() || undefined }),
                  ).then((ok) => ok && setComment(""));
                }}>
                  Accept
                </Button>
                <Button disabled={working || !canRejectPlan(run, me) || !comment.trim()} onClick={() => {
                  const hash = planHash(run);
                  if (!hash) return;
                  void act("reject the plan", () =>
                    api.approve(runId, { artifact: "ExecutionPlan", action: "REJECT", expected_hash: hash, comment: comment.trim() }),
                  ).then((ok) => ok && setComment(""));
                }}>
                  Reject
                </Button>
                {blocked && <Caption1 className={styles.label}>Accept is unavailable: {blocked}</Caption1>}
              </div>
            </section>

            {execution && (
              <section className={styles.card} aria-label="Data engineering actions">
                <Subtitle2>Data-engineering actions</Subtitle2>
                {execution.data_engineering_actions.length === 0 ? (
                  <Body1>None needed.</Body1>
                ) : (
                  <ul className={styles.list}>
                    {execution.data_engineering_actions.map((action) => (
                      <li key={action.id} className={styles.item}>
                        <div className={styles.actions}>
                          <Body1Strong>{action.id}</Body1Strong>
                          <Badge color={action.status === "done" ? "success" : action.blocking ? "danger" : "warning"}>
                            {action.status === "done" ? "Done" : action.blocking ? "Blocking" : "Open"}
                          </Badge>
                          <Caption1 className={styles.label}>Owner: {action.owner ?? "unassigned"}</Caption1>
                        </div>
                        <Body1>{action.description}</Body1>
                      </li>
                    ))}
                  </ul>
                )}
                {blockingActions(execution).length > 0 && (
                  <Caption1 className={styles.label}>Blocking actions must be done before the plan can be accepted.</Caption1>
                )}
              </section>
            )}
          </div>
        </div>
      )}

      {model && (
        <section className={styles.card} aria-label="Tables">
          <Subtitle2>Tables</Subtitle2>
          <ul className={styles.list}>
            {model.tables.map((table) => (
              <li key={table.name} className={styles.item}>
                <div className={styles.actions}>
                  <Body1Strong>{table.name}</Body1Strong>
                  <Badge appearance="outline">{table.role}</Badge>
                  {table.source && (
                    <Caption1 className={styles.label}>
                      {table.source.schema}.{table.source.table}
                    </Caption1>
                  )}
                  {table.grain && <Caption1 className={styles.label}>grain: {table.grain}</Caption1>}
                </div>
                <Caption1 className={styles.detail}>Why: {tableRationale(table, resolution)}</Caption1>
              </li>
            ))}
          </ul>
          {rejected.length > 0 && <Body1Strong>Rejected alternatives</Body1Strong>}
          <ul className={styles.list}>
            {rejected.map((item) => (
              <li key={`${item.entity}-${item.table}`} className={styles.item}>
                <div className={styles.actions}>
                  <Body1>{item.table}</Body1>
                  <Caption1 className={styles.label}>
                    for {item.entity} · score {item.score}
                  </Caption1>
                  {item.requiresException && <Badge color="warning">Needs exception</Badge>}
                </div>
                <Caption1 className={styles.detail}>{item.reason}</Caption1>
              </li>
            ))}
          </ul>
          {unresolved.map((item) => (
            <MessageBar key={item.entity} intent="warning">
              <MessageBarBody>
                {item.entity}: {item.reason}
              </MessageBarBody>
            </MessageBar>
          ))}
        </section>
      )}

      {model && (
        <section className={styles.card} aria-label="Relationships">
          <Subtitle2>Relationships</Subtitle2>
          {model.relationships.length === 0 && <Body1>No relationships.</Body1>}
          <ul className={styles.list}>
            {model.relationships.map((rel) => {
              const evidence = evidenceFor(rel, inventory);
              const rationale = rationales[rel.name] ?? "";
              return (
                <li key={rel.name} className={styles.item}>
                  <div className={styles.actions}>
                    <Body1Strong>{rel.name}</Body1Strong>
                    <Caption1 className={styles.label}>
                      {rel.from.table}.{rel.from.column} → {rel.to.table}.{rel.to.column} · {rel.cardinality.replace(/_/g, " ")}
                    </Caption1>
                    <Button
                      size="small"
                      disabled={working || !editable}
                      onClick={() => edit("remove the relationship", [removeRelationship(rel.name)])}
                    >
                      Remove
                    </Button>
                  </div>
                  <Caption1 className={styles.detail}>Evidence: {evidenceSummary(rel, evidence)}</Caption1>
                  {evidence && evidence.evidence.queries.length > 0 && (
                    <details>
                      <summary>Show SQL</summary>
                      <pre className={styles.queries}>{evidence.evidence.queries.join("\n\n")}</pre>
                    </details>
                  )}
                  {editable && (
                    <div className={styles.actions}>
                      <Input
                        size="small"
                        aria-label={`Override rationale for ${rel.name}`}
                        placeholder="Rationale to override the evidence"
                        value={rationale}
                        disabled={working}
                        onChange={(_, data) => setRationales({ ...rationales, [rel.name]: data.value })}
                      />
                      <Button
                        size="small"
                        disabled={working || !canOverride(rationale)}
                        onClick={() => edit("override the relationship", [overrideRelationship(rel.name, rationale)])}
                      >
                        Override
                      </Button>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      )}

      {model && (
        <section className={styles.card} aria-label="Measures">
          <Subtitle2>Measures</Subtitle2>
          {model.measures.length === 0 && <Body1>No measures.</Body1>}
          <ul className={styles.list}>
            {model.measures.map((measure) => (
              <li key={measure.name} className={styles.item}>
                <div className={styles.actions}>
                  <Body1Strong>{measure.name}</Body1Strong>
                  <Caption1 className={styles.label}>
                    on {measure.table}
                    {measure.format_string ? ` · ${measure.format_string}` : ""}
                  </Caption1>
                  <Button
                    size="small"
                    disabled={working || !editable}
                    onClick={() => edit("remove the measure", [removeMeasure(measure.name)])}
                  >
                    Remove
                  </Button>
                </div>
                <Caption1 className={styles.detail}>
                  Formula intent: {measure.formula_intent ?? "none"} · DAX {measure.dax ?? "is generated later"}
                </Caption1>
              </li>
            ))}
          </ul>
          <Body1Strong>Row-level security</Body1Strong>
          {model.roles.length === 0 ? (
            <Body1>No RLS roles: the contract has no security requirements.</Body1>
          ) : (
            <ul className={styles.list}>
              {model.roles.map((role) => (
                <li key={role.name}>
                  <Body1>
                    {role.name}: {role.description}
                  </Body1>
                  <div>
                    <Caption1 className={styles.label}>Applies to {role.applies_to.join(", ") || "unspecified"}</Caption1>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {history.length > 0 && (
        <section className={styles.card} aria-label="History">
          <Subtitle2>History</Subtitle2>
          <ul className={styles.list}>
            {history.map((record) => (
              <li key={record.approval_id}>
                <Body1>
                  {record.action} (version {record.artifact_version}){record.comment ? `: ${record.comment}` : ""}
                </Body1>
                <div>
                  <Caption1 className={styles.label}>
                    {record.user.upn} · {record.role ?? "no role"} · {formatDateTime(record.at)}
                  </Caption1>
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
