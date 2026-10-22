// Model review and approval (T-507): the review data (tables, relationships,
// each measure's DAX next to its approved KPI definition and reconciliation,
// RLS roles and the latest ValidationReport), the Gate G4 checklist, and the
// per-role approval actions (bi_engineer, business_owner, and
// security_reviewer when the model has RLS roles). Generating the TMDL is a
// deterministic udf call (api.generateModel); adding measures, validating and
// repairing call the model or a Fabric/SQL endpoint, so they are jobs
// (api.addMeasures/validateModel/repairModel). Ported in spirit from
// rayfin-ui's src/app/runs/[id]/model/page.tsx to this workload's pages (T-104).
import {
  Badge,
  Body1,
  Body1Strong,
  Button,
  Caption1,
  Checkbox,
  Field,
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
import { useCallback, useEffect, useState } from "react";

import { ErrorBanner } from "../components/ErrorBanner.tsx";
import { JobProgress } from "../components/JobProgress.tsx";
import { StateBadge } from "../components/StateBadge.tsx";
import { type ApiError, api } from "../lib/api.ts";
import { formatDateTime } from "../lib/format.ts";
import {
  actableRoles,
  approveBlockedReason,
  canApproveAs,
  canRejectAs,
  definitionLines,
  filterDirection,
  isOwner,
  MODEL_STALE_MESSAGE,
  needsValidation,
  reconciliationColor,
  reconciliationRows,
  roleLabel,
  roleScope,
  statusColor,
  statusLabel,
} from "../lib/model.ts";
import { isStale } from "../lib/review.ts";
import type { Me, ModelReview, RunDetail } from "../lib/types.ts";

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
  dax: { fontFamily: tokens.fontFamilyMonospace, fontSize: tokens.fontSizeBase200, whiteSpace: "pre-wrap" },
  roleRow: { display: "flex", flexDirection: "column", rowGap: "6px", borderTop: `1px solid ${tokens.colorNeutralStroke2}`, paddingTop: "8px" },
});

export function ModelPage({ runId }: { runId: string }) {
  const styles = useStyles();
  const [run, setRun] = useState<RunDetail | null>(null);
  const [me, setMe] = useState<Me | null>(null);
  const [review, setReview] = useState<ModelReview | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<{ title: string; error: ApiError } | null>(null);
  const [stale, setStale] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [comment, setComment] = useState("");
  const [definitionWrong, setDefinitionWrong] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setStale(false);
    try {
      const [loaded, who] = await Promise.all([api.getRun(runId), api.getMe()]);
      setRun(loaded);
      setMe(who);
      try {
        setReview(await api.getModelReview(runId));
      } catch (e) {
        if ((e as ApiError).status === 404) {
          setReview(null);
        } else {
          throw e;
        }
      }
    } catch (e) {
      setError({ title: "Couldn't load the model.", error: e as ApiError });
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

  function decide(role: string, action: "ACCEPT" | "REJECT") {
    if (!review) return;
    const wrong = action === "REJECT" && isOwner(role) && definitionWrong;
    void act(`${action === "ACCEPT" ? "approve" : "reject"} as ${roleLabel(role)}`, () =>
      api.approve(runId, {
        artifact: "SemanticModel",
        action,
        expected_hash: review.model_hash,
        role,
        comment: comment.trim() || undefined,
        definition_wrong: wrong || undefined,
      }),
    ).then((ok) => {
      if (ok) {
        setComment("");
        setDefinitionWrong(false);
      }
    });
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
  const roles = review?.approvals_required ?? [];
  const actable = actableRoles(review, me);

  return (
    <div className={styles.page}>
      <div className={styles.titleRow}>
        <Title2 as="h2">Model review</Title2>
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
            {MODEL_STALE_MESSAGE} Nothing was changed.
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
      {loading && !run && <Spinner label="Loading model…" />}

      {run && (
        <section className={styles.card} aria-label="Summary">
          <div className={styles.titleRow}>
            <StateBadge state={run.state} />
            {review && (
              <>
                <Badge color={review.state === "MODEL_APPROVED" ? "success" : "informative"}>
                  {review.state === "MODEL_APPROVED" ? "Approved" : "Draft"}
                </Badge>
                <Caption1 className={styles.label}>Version {review.version}</Caption1>
                <span className={styles.mono}>{review.model_hash}</span>
                {needsValidation(review) && (
                  <Button
                    size="small"
                    disabled={working}
                    onClick={() => void act("validate the model", () => api.validateModel(runId)).then((ok) => ok && setNotice("Validation finished."))}
                  >
                    Validate again
                  </Button>
                )}
              </>
            )}
          </div>
          {review ? (
            <Body1>
              {review.tables.length} tables, {review.relationships.length} relationships, {review.measures.length} measures,{" "}
              {review.roles.length} RLS roles.
            </Body1>
          ) : (
            <Body1>No model has been generated for this run yet.</Body1>
          )}
        </section>
      )}

      {run && (
        <section className={styles.card} aria-label="Build the model">
          <Subtitle2>Build the model</Subtitle2>
          <Body1>Generate the TMDL from the approved plan, add the DAX measures, then validate (Gate G4).</Body1>
          <div className={styles.actions}>
            <Button
              disabled={working || run.state !== "PLAN_APPROVED"}
              onClick={() => void act("generate the model", () => api.generateModel(runId))}
            >
              {review ? "Regenerate model" : "Generate model"}
            </Button>
            <Button
              disabled={working || run.state !== "MODEL_GENERATING"}
              onClick={() =>
                void act("add the measures", () => api.addMeasures(runId)).then((ok) => ok && setNotice("Measures generated."))
              }
            >
              Add measures
            </Button>
            <Button
              disabled={working || run.state !== "MODEL_GENERATING"}
              onClick={() => void act("validate the model", () => api.validateModel(runId)).then((ok) => ok && setNotice("Validation finished."))}
            >
              Validate
            </Button>
            <Button
              disabled={working || run.state !== "MODEL_FAILED"}
              onClick={() => void act("repair the model", () => api.repairModel(runId)).then((ok) => ok && setNotice("Repair finished."))}
            >
              Repair
            </Button>
          </div>
          {run.state === "MODEL_FAILED" && <MessageBar intent="warning"><MessageBarBody>Validation failed. Run Repair, or fix the issue and Validate again.</MessageBarBody></MessageBar>}
        </section>
      )}

      {review && (
        <div className={styles.columns}>
          <div className={styles.stack}>
            <section className={styles.card} aria-label="Gate G4">
              <Subtitle2>Gate G4 checklist</Subtitle2>
              <ul className={styles.checks}>
                {review.gate.checks.map((check) => (
                  <li key={check.id} className={styles.check}>
                    <Badge color={check.status === "pass" ? "success" : check.status === "fail" ? "danger" : "warning"}>
                      {check.status === "pass" ? "Pass" : check.status === "fail" ? "Fail" : "Warning"}
                    </Badge>
                    <Body1Strong>{check.id}</Body1Strong>
                    <Body1>{check.description}</Body1>
                  </li>
                ))}
              </ul>
              {!review.validation_current && (
                <MessageBar intent="warning">
                  <MessageBarBody>This checklist is from an earlier version of the model. Validate again.</MessageBarBody>
                </MessageBar>
              )}
            </section>
          </div>

          <div className={styles.stack}>
            <section className={styles.card} aria-label="Approvals">
              <Subtitle2>Approvals</Subtitle2>
              {roles.map((role) => {
                const status = review.approvals_status.find((s) => s.role === role);
                const blocked = approveBlockedReason(review, me, role);
                return (
                  <div key={role} className={styles.roleRow}>
                    <div className={styles.actions}>
                      <Body1Strong>{roleLabel(role)}</Body1Strong>
                      {status && <Badge color={statusColor(status.status)}>{statusLabel(status)}</Badge>}
                    </div>
                    <Caption1 className={styles.label}>{roleScope(role)}</Caption1>
                    {actable.includes(role) && (
                      <>
                        {isOwner(role) && (
                          <Checkbox
                            label="This Reject means the KPI definition itself is wrong (sends the run back to requirement analysis)"
                            checked={definitionWrong}
                            onChange={(_, data) => setDefinitionWrong(!!data.checked)}
                          />
                        )}
                        <div className={styles.actions}>
                          <Button
                            appearance="primary"
                            size="small"
                            disabled={working || !canApproveAs(review, me, role)}
                            onClick={() => decide(role, "ACCEPT")}
                          >
                            Approve as {roleLabel(role)}
                          </Button>
                          <Button
                            size="small"
                            disabled={working || !canRejectAs(review, me, role) || !comment.trim()}
                            onClick={() => decide(role, "REJECT")}
                          >
                            Reject as {roleLabel(role)}
                          </Button>
                          {blocked && <Caption1 className={styles.label}>{blocked}</Caption1>}
                        </div>
                      </>
                    )}
                  </div>
                );
              })}
              {actable.length > 0 && (
                <Field label="Comment" hint="Needed to reject; optional to approve.">
                  <Textarea value={comment} resize="vertical" maxLength={2000} disabled={working} onChange={(_, data) => setComment(data.value)} />
                </Field>
              )}
              {roles.length === 0 && <Body1>No approvals required.</Body1>}
            </section>
          </div>
        </div>
      )}

      {review && (
        <section className={styles.card} aria-label="Tables">
          <Subtitle2>Tables</Subtitle2>
          <ul className={styles.list}>
            {review.tables.map((table) => (
              <li key={table.name} className={styles.item}>
                <div className={styles.actions}>
                  <Body1Strong>{table.name}</Body1Strong>
                  <Badge appearance="outline">{table.role}</Badge>
                  {table.source && <Caption1 className={styles.label}>{table.source}</Caption1>}
                  {table.grain && <Caption1 className={styles.label}>grain: {table.grain}</Caption1>}
                  {table.hierarchies.length > 0 && <Caption1 className={styles.label}>hierarchies: {table.hierarchies.join(", ")}</Caption1>}
                </div>
                <Caption1 className={styles.detail}>
                  {table.columns.map((c) => `${c.name} (${c.data_type}${c.is_hidden ? ", hidden" : ""})`).join(", ")}
                </Caption1>
              </li>
            ))}
          </ul>
        </section>
      )}

      {review && (
        <section className={styles.card} aria-label="Relationships">
          <Subtitle2>Relationships</Subtitle2>
          {review.relationships.length === 0 && <Body1>No relationships.</Body1>}
          <ul className={styles.list}>
            {review.relationships.map((rel) => (
              <li key={rel.name} className={styles.item}>
                <div className={styles.actions}>
                  <Body1Strong>{rel.name}</Body1Strong>
                  <Caption1 className={styles.label}>
                    {rel.from.table}.{rel.from.column} → {rel.to.table}.{rel.to.column} · {rel.cardinality.replace(/_/g, " ")} ·{" "}
                    {filterDirection(rel.cross_filter)}
                  </Caption1>
                </div>
                {rel.override_rationale && <Caption1 className={styles.detail}>Override: {rel.override_rationale}</Caption1>}
              </li>
            ))}
          </ul>
        </section>
      )}

      {review && (
        <section className={styles.card} aria-label="Measures">
          <Subtitle2>Measures</Subtitle2>
          {review.measures.length === 0 && <Body1>No measures.</Body1>}
          <ul className={styles.list}>
            {review.measures.map((measure) => (
              <li key={measure.name} className={styles.item}>
                <div className={styles.actions}>
                  <Body1Strong>{measure.name}</Body1Strong>
                  <Caption1 className={styles.label}>on {measure.table}</Caption1>
                  <Badge color={reconciliationColor(measure.reconciliation.status)}>{measure.reconciliation.status}</Badge>
                </div>
                {measure.description && <Caption1 className={styles.detail}>{measure.description}</Caption1>}
                <pre className={styles.dax}>{measure.dax ?? "(not generated yet)"}</pre>
                <Body1Strong>Approved definition</Body1Strong>
                {definitionLines(measure).map((line) => (
                  <Caption1 key={line} className={styles.detail}>
                    {line}
                  </Caption1>
                ))}
                <Body1Strong>Reconciliation</Body1Strong>
                <div className={styles.actions}>
                  {reconciliationRows(measure.reconciliation).map((row) => (
                    <Caption1 key={row.label} className={styles.label}>
                      {row.label}: {row.value}
                    </Caption1>
                  ))}
                </div>
                {measure.reconciliation.message && <Caption1 className={styles.detail}>{measure.reconciliation.message}</Caption1>}
              </li>
            ))}
          </ul>
        </section>
      )}

      {review && review.roles.length > 0 && (
        <section className={styles.card} aria-label="Row-level security">
          <Subtitle2>Row-level security</Subtitle2>
          <ul className={styles.list}>
            {review.roles.map((role) => (
              <li key={role.name}>
                <Body1>
                  {role.name}: {role.description}
                </Body1>
                <Caption1 className={styles.label}>Applies to {role.applies_to.join(", ") || "unspecified"}</Caption1>
              </li>
            ))}
          </ul>
        </section>
      )}

      {review && review.approvals_done.length > 0 && (
        <section className={styles.card} aria-label="History">
          <Subtitle2>History</Subtitle2>
          <ul className={styles.list}>
            {review.approvals_done.map((record) => (
              <li key={record.approval_id}>
                <div className={styles.actions}>
                  <Body1>
                    {record.action} as {record.role ?? "no role"} (version {record.artifact_version})
                    {record.comment ? `: ${record.comment}` : ""}
                  </Body1>
                  {!record.current && <Badge color="warning">{record.note ?? "invalidated"}</Badge>}
                </div>
                <Caption1 className={styles.label}>
                  {record.user.upn} · {formatDateTime(record.at)}
                </Caption1>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
