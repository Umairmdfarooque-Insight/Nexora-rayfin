// Contract review (T-205 FR-2-5): each KPI's formula intent, grain, filters,
// time logic, exclusions, currency and examples, plus dimensions, filters,
// security, locked facts and assumptions; the Gate G1 checklist; the approval
// actions (Accept, Edit, Reject, Explain, Regenerate) and the approval history.
import {
  Badge,
  Body1,
  Body1Strong,
  Button,
  Caption1,
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

import { KpiCard } from "../components/KpiCard.tsx";
import { LongText } from "../components/LongText.tsx";
import { ErrorBanner } from "../components/ErrorBanner.tsx";
import { StateBadge } from "../components/StateBadge.tsx";
import { type ApiError, api } from "../lib/api.ts";
import { formatDateTime } from "../lib/format.ts";
import {
  acceptBlockedReason,
  canAccept,
  canChangeDraft,
  contractHash,
  hasRole,
  isLocked,
  isStale,
  OWNER_ROLE,
  STALE_MESSAGE,
} from "../lib/review.ts";
import type { ApprovalAction, ApprovalRecord, Explanation, GateResult, KpiChanges, Me, RunDetail } from "../lib/types.ts";

const useStyles = makeStyles({
  page: { display: "flex", flexDirection: "column", rowGap: "12px" },
  titleRow: { display: "flex", alignItems: "center", columnGap: "12px", flexWrap: "wrap" },
  mono: { fontFamily: tokens.fontFamilyMonospace, color: tokens.colorNeutralForeground3 },
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
  label: { color: tokens.colorNeutralForeground3 },
  rationale: {
    backgroundColor: tokens.colorNeutralBackground3,
    borderRadius: tokens.borderRadiusMedium,
    padding: "6px 10px",
  },
  history: { listStyleType: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", rowGap: "8px" },
});

function List({ label, items }: { label: string; items: string[] }) {
  return (
    <Body1>
      <Body1Strong>{label}: </Body1Strong>
      {items.length > 0 ? items.join(", ") : "None"}
    </Body1>
  );
}

export function ContractPage({ runId }: { runId: string }) {
  const styles = useStyles();
  const [run, setRun] = useState<RunDetail | null>(null);
  const [me, setMe] = useState<Me | null>(null);
  const [gate, setGate] = useState<GateResult | null>(null);
  const [approvals, setApprovals] = useState<ApprovalRecord[]>([]);
  const [explanation, setExplanation] = useState<Explanation | null>(null);
  const [comment, setComment] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<{ title: string; error: ApiError } | null>(null);
  const [stale, setStale] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setStale(false);
    try {
      const [loaded, who, records] = await Promise.all([api.getRun(runId), api.getMe(), api.listApprovals(runId)]);
      setRun(loaded);
      setMe(who);
      setApprovals(records);
      // G1 is only meaningful for a contract that can still change.
      setGate(loaded.contract && !isLocked(loaded.contract) ? await api.checkG1(runId) : null);
    } catch (e) {
      setError({ title: "Couldn't load the contract.", error: e as ApiError });
    } finally {
      setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    void load();
  }, [load]);

  /** Runs one approval action. Returns true when the API accepted it. */
  async function act(
    action: ApprovalAction,
    extra: { comment?: string; edits?: { kpis: Record<string, KpiChanges> } } = {},
  ): Promise<boolean> {
    const hash = contractHash(run);
    if (!hash) return false;
    setBusy(action);
    setError(null);
    try {
      const result = await api.approve(runId, {
        artifact: "BusinessContract",
        action,
        expected_hash: hash,
        ...extra,
      });
      setRun(result.run);
      setApprovals(await api.listApprovals(runId));
      if (action === "EXPLAIN") {
        setExplanation(result.explanation);
      } else {
        setExplanation(null);
        setComment("");
        setGate(result.gate_result ?? (result.run.contract && !isLocked(result.run.contract) ? await api.checkG1(runId) : null));
      }
      return true;
    } catch (e) {
      const failure = e as ApiError;
      if (isStale(failure)) {
        // Nothing was changed. The user reloads to see the latest version.
        setStale(true);
      } else {
        setError({ title: `Couldn't ${action.toLowerCase()} the contract.`, error: failure });
        if (failure.code === "GATE_FAILED") {
          try {
            setGate(await api.checkG1(runId));
          } catch {
            // The error banner is already showing.
          }
        }
      }
      return false;
    } finally {
      setBusy(null);
    }
  }

  const saveKpi = (name: string, changes: KpiChanges) => act("EDIT", { edits: { kpis: { [name]: changes } } });

  if (error?.error.status === 404 && !run) {
    return (
      <div className={styles.page}>
        <MessageBar intent="warning" role="alert">
          <MessageBarBody>Run not found: {runId}</MessageBarBody>
        </MessageBar>
      </div>
    );
  }

  const contract = run?.contract ?? null;
  const locked = isLocked(contract);
  const editable = canChangeDraft(run);
  const isOwner = hasRole(me, OWNER_ROLE);
  const blockedReason = acceptBlockedReason(run, gate, me);
  const working = busy !== null || loading;

  return (
    <div className={styles.page}>
      <div className={styles.titleRow}>
        <Title2 as="h2">Contract review</Title2>
        <Button size="small" onClick={() => void load()} disabled={working}>
          Reload
        </Button>
        {loading && <Spinner size="tiny" />}
      </div>

      {stale && (
        <MessageBar intent="warning" role="alert">
          <MessageBarBody>
            <MessageBarTitle>Out of date.</MessageBarTitle>
            {STALE_MESSAGE} Nothing was changed.
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
      {loading && !run && <Spinner label="Loading contract…" />}

      {run && !contract && (
        <MessageBar intent="info">
          <MessageBarBody>There is no contract yet. Compile the requirement first, on the Overview tab.</MessageBarBody>
        </MessageBar>
      )}

      {run && contract && (
        <>
          <section className={styles.card} aria-label="Summary">
            <div className={styles.titleRow}>
              <StateBadge state={run.state} />
              <Badge color={locked ? "success" : "informative"}>{locked ? "Approved and locked" : "Draft"}</Badge>
              <Caption1 className={styles.label}>Version {contract.version}</Caption1>
              <span className={styles.mono}>{contractHash(run)}</span>
            </div>
            <Body1Strong>Objective</Body1Strong>
            <LongText text={contract.business_objective} />
            {locked && (
              <MessageBar intent="success">
                <MessageBarBody>This contract is approved and locked. It can't be edited.</MessageBarBody>
              </MessageBar>
            )}
          </section>

          <div className={styles.columns}>
            <section className={styles.card} aria-label="Gate G1">
              <Subtitle2>Gate G1 checklist</Subtitle2>
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
                <Body1>{locked ? "Gate G1 passed when the contract was approved." : "Not checked yet."}</Body1>
              )}
            </section>

            <section className={styles.card} aria-label="Approval actions">
              <Subtitle2>Review</Subtitle2>
              {editable && (
                <Field label="Comment" hint="Required to reject. Used as guidance when regenerating.">
                  <Textarea
                    value={comment}
                    resize="vertical"
                    maxLength={2000}
                    disabled={working}
                    onChange={(_, data) => setComment(data.value)}
                  />
                </Field>
              )}
              <div className={styles.actions}>
                {editable && isOwner && (
                  <>
                    <Button
                      appearance="primary"
                      disabled={working || !canAccept(run, gate, me)}
                      onClick={() => void act("ACCEPT", comment.trim() ? { comment: comment.trim() } : {})}
                    >
                      Accept
                    </Button>
                    <Button disabled={working || !comment.trim()} onClick={() => void act("REJECT", { comment: comment.trim() })}>
                      Reject
                    </Button>
                  </>
                )}
                {editable && (
                  <Button
                    disabled={working}
                    onClick={() => void act("REGENERATE", comment.trim() ? { comment: comment.trim() } : {})}
                  >
                    Regenerate
                  </Button>
                )}
                <Button disabled={working} onClick={() => void act("EXPLAIN")}>
                  Explain
                </Button>
                {busy && (
                  <Spinner
                    size="tiny"
                    labelPosition="after"
                    label={busy === "REGENERATE" ? "Regenerating. This can take up to a minute…" : "Working…"}
                  />
                )}
              </div>
              {editable && !isOwner && (
                <Caption1 className={styles.label}>Accept and Reject need the {OWNER_ROLE} role, so they are hidden for you.</Caption1>
              )}
              {editable && isOwner && blockedReason && (
                <Caption1 className={styles.label}>Accept is unavailable: {blockedReason}</Caption1>
              )}
            </section>
          </div>

          <Subtitle2>KPIs</Subtitle2>
          {contract.kpis.length === 0 && <Body1>The contract has no KPI yet.</Body1>}
          {contract.kpis.map((kpi) => (
            <KpiCard
              key={kpi.name}
              kpi={kpi}
              canEdit={editable}
              busy={working}
              rationale={explanation?.kpis.find((k) => k.name === kpi.name)?.rationale}
              onSave={saveKpi}
            />
          ))}

          <div className={styles.columns}>
            <section className={styles.card} aria-label="Scope of the contract">
              <Subtitle2>Contract details</Subtitle2>
              <List label="Dimensions" items={contract.dimensions} />
              <List label="Filters" items={contract.filters} />
              <List label="Entities" items={contract.entities} />
              <List label="Exclusions" items={contract.exclusions} />
              <Body1>
                <Body1Strong>Grain: </Body1Strong>
                {contract.grain ?? "Not set"}
              </Body1>
              <Body1>
                <Body1Strong>Currency: </Body1Strong>
                {contract.currency ?? "Not set"}
              </Body1>
              <Body1Strong>Security</Body1Strong>
              {contract.security.length === 0 ? (
                <Body1>None</Body1>
              ) : (
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {contract.security.map((item, index) => (
                    <li key={index}>
                      {item.description}
                      {item.applies_to.length > 0 ? ` (${item.applies_to.join(", ")})` : ""}
                    </li>
                  ))}
                </ul>
              )}
              <Body1Strong>Assumptions</Body1Strong>
              {contract.assumptions.length === 0 ? (
                <Body1>None</Body1>
              ) : (
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {contract.assumptions.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              )}
            </section>

            <div className={styles.stack}>
              <section className={styles.card} aria-label="Locked facts">
                <Subtitle2>Locked facts</Subtitle2>
                {contract.locked_facts.length === 0 && <Body1>None</Body1>}
                {contract.locked_facts.map((fact) => (
                  <div key={fact.id}>
                    <Caption1 className={styles.label}>
                      {fact.id}
                      {fact.source_question_id ? ` · from ${fact.source_question_id}` : ""} · {formatDateTime(fact.locked_at)}
                    </Caption1>
                    <LongText text={fact.text} />
                  </div>
                ))}
              </section>

              <section className={styles.card} aria-label="Questions">
                <Subtitle2>Questions</Subtitle2>
                {contract.open_questions.length === 0 && <Body1>None</Body1>}
                {contract.open_questions.map((q) => (
                  <div key={q.id}>
                    <Body1Strong>{q.text}</Body1Strong>{" "}
                    <Badge appearance="outline" color={q.status === "open" ? (q.blocking ? "danger" : "warning") : "success"}>
                      {q.status === "open" ? (q.blocking ? "Open, blocking" : "Open") : q.status}
                    </Badge>
                    {q.answer && <LongText text={`Answer: ${q.answer}`} />}
                    {q.dismiss_reason && <LongText text={`Dismissed: ${q.dismiss_reason}`} />}
                    {explanation?.questions.find((x) => x.id === q.id) && (
                      <div className={styles.rationale}>
                        <LongText text={explanation.questions.find((x) => x.id === q.id)!.rationale} />
                      </div>
                    )}
                  </div>
                ))}
              </section>
            </div>
          </div>
        </>
      )}

      {run && (
        <section className={styles.card} aria-label="Approval history">
          <Subtitle2>Approval history</Subtitle2>
          {approvals.length === 0 ? (
            <Body1>No approval actions yet.</Body1>
          ) : (
            <ol className={styles.history}>
              {[...approvals].reverse().map((record) => (
                <li key={record.approval_id}>
                  <Body1Strong>{record.action}</Body1Strong>{" "}
                  <Caption1 className={styles.label}>
                    {record.user.upn}
                    {record.role ? ` (${record.role})` : ""} · {formatDateTime(record.at)} · v{record.artifact_version}
                  </Caption1>
                  {record.comment && <LongText text={record.comment} />}
                </li>
              ))}
            </ol>
          )}
        </section>
      )}
    </div>
  );
}
