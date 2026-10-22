"use client";

import {
  Body1,
  Body1Strong,
  Button,
  Caption1,
  Divider,
  makeStyles,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Spinner,
  Subtitle2,
  Title2,
  tokens,
} from "@fluentui/react-components";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ClarificationPanel } from "@/components/ClarificationPanel";
import { ErrorBanner } from "@/components/ErrorBanner";
import { StateBadge } from "@/components/StateBadge";
import { api, type ApiError } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import type { RunDetail } from "@/lib/types";

const useStyles = makeStyles({
  page: { display: "flex", flexDirection: "column", rowGap: "12px" },
  titleRow: { display: "flex", alignItems: "center", columnGap: "12px", flexWrap: "wrap" },
  runId: { fontFamily: tokens.fontFamilyMonospace, color: tokens.colorNeutralForeground3 },
  // State, next action and blockers sit in one row at the top so they are
  // visible without scrolling at 1366x768.
  status: {
    display: "grid",
    gridTemplateColumns: "repeat(3, 1fr)",
    gap: "16px",
  },
  card: {
    backgroundColor: tokens.colorNeutralBackground1,
    borderRadius: tokens.borderRadiusLarge,
    boxShadow: tokens.shadow4,
    padding: "12px 16px",
    display: "flex",
    flexDirection: "column",
    rowGap: "6px",
    minWidth: 0,
  },
  columns: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: "16px", alignItems: "start" },
  stack: { display: "flex", flexDirection: "column", rowGap: "12px", minWidth: 0 },
  prompt: { whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: "140px", overflowY: "auto" },
  timeline: {
    listStyleType: "none",
    margin: 0,
    padding: 0,
    maxHeight: "320px",
    overflowY: "auto",
    display: "flex",
    flexDirection: "column",
    rowGap: "10px",
  },
  entry: {
    paddingLeft: "12px",
    borderLeft: `3px solid ${tokens.colorBrandStroke1}`,
  },
  label: { color: tokens.colorNeutralForeground3 },
  mono: { fontFamily: tokens.fontFamilyMonospace, overflowWrap: "anywhere" },
});

export default function RunDetailPage() {
  const styles = useStyles();
  const { id } = useParams<{ id: string }>();
  const [run, setRun] = useState<RunDetail | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setRun(await api.getRun(id));
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  if (error?.status === 404) {
    return (
      <div className={styles.page}>
        <MessageBar intent="warning" role="alert">
          <MessageBarBody>Run not found: {id}</MessageBarBody>
        </MessageBar>
        <Link href="/runs">Back to runs</Link>
      </div>
    );
  }

  return (
    <div className={styles.page}>
      {error && (
        <ErrorBanner
          title="Couldn't load this run."
          error={error}
          onRetry={load}
          disabled={loading}
        />
      )}
      {loading && !run && <Spinner label="Loading run…" />}

      {run && (
        <>
          <div className={styles.titleRow}>
            <Title2 as="h1">Run</Title2>
            <span className={styles.runId}>{run.run_id}</span>
            <Caption1 className={styles.label}>
              Created by: {run.created_by?.upn ?? "unknown"}
            </Caption1>
            <Button size="small" onClick={load} disabled={loading}>
              Refresh
            </Button>
            {loading && <Spinner size="tiny" />}
          </div>

          <section className={styles.status} aria-label="Status">
            <div className={styles.card}>
              <Caption1 className={styles.label}>Current state</Caption1>
              <div>
                <StateBadge state={run.state} />
              </div>
              {run.resume_state && (
                <Caption1>Returns to {run.resume_state} when input is provided</Caption1>
              )}
            </div>
            <div className={styles.card}>
              <Caption1 className={styles.label}>Next action</Caption1>
              <Subtitle2>{run.next_action}</Subtitle2>
            </div>
            <div className={styles.card}>
              <Caption1 className={styles.label}>Open questions</Caption1>
              {run.open_questions.length === 0 ? (
                <Body1>None</Body1>
              ) : (
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {run.open_questions.map((question) => (
                    <li key={question.id}>
                      {question.text}
                      {question.blocking ? " (blocking)" : ""}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </section>

          <ClarificationPanel run={run} onRun={setRun} />

          {(run.artifacts.MetadataInventory ||
            run.state === "REQUIREMENT_APPROVED" ||
            run.data_gaps.length > 0) && (
            <section className={styles.card} aria-label="Data">
              <Subtitle2>Data</Subtitle2>
              <Body1>
                <Link href={`/runs/${encodeURIComponent(run.run_id)}/inventory`}>Open the inventory</Link>{" "}
                to harvest, profile and review the Lakehouse metadata.
              </Body1>
              {run.data_gaps.map((gap) => (
                <MessageBar key={gap.gap_id} intent="warning">
                  <MessageBarBody>
                    <MessageBarTitle>Data gap</MessageBarTitle>
                    {gap.description} ({gap.raised_by}, {formatDateTime(gap.at)})
                  </MessageBarBody>
                </MessageBar>
              ))}
            </section>
          )}

          {(run.artifacts.ExecutionPlan || run.state === "DATA_PROFILED" || run.state === "PLAN_APPROVED") && (
            <section className={styles.card} aria-label="Plan">
              <Subtitle2>Plan</Subtitle2>
              <Body1>
                <Link href={`/runs/${encodeURIComponent(run.run_id)}/plan`}>Open the plan review</Link> to see why each
                table was chosen, the relationships and their evidence, and to approve the plan.
              </Body1>
            </section>
          )}

          {(run.artifacts.TMDLModel || run.state.startsWith("MODEL_")) && (
            <section className={styles.card} aria-label="Model">
              <Subtitle2>Semantic model</Subtitle2>
              <Body1>
                <Link href={`/runs/${encodeURIComponent(run.run_id)}/model`}>Open the model review</Link> to compare each
                measure with its approved definition, see the validation, and approve the model by role.
              </Body1>
            </section>
          )}

          {(run.artifacts.PbirReport || run.state.startsWith("REPORT_")) && (
            <section className={styles.card} aria-label="Report">
              <Subtitle2>Report</Subtitle2>
              <Body1>
                <Link href={`/runs/${encodeURIComponent(run.run_id)}/report`}>Open the report review</Link> to see each
                rendered page and the questions its visuals answer, regenerate a visual, and approve the report.
              </Body1>
            </section>
          )}

          <section className={styles.card} aria-label="Audit">
            <Subtitle2>Audit trail</Subtitle2>
            <Body1>
              <Link href={`/runs/${encodeURIComponent(run.run_id)}/audit`}>Open the audit trail</Link> to see every stage, who
              approved it and when, with the artifact hashes. From the report review, each visual has a Trace link back to its
              measure, approved KPI, source columns and the prompt.
            </Body1>
          </section>

          <div className={styles.columns}>
            <div className={styles.stack}>
              <section className={styles.card} aria-label="Prompt">
                <Subtitle2>Prompt</Subtitle2>
                <Body1 className={styles.prompt}>{run.prompt}</Body1>
                {(run.subject_area ||
                  run.desired_outputs.length > 0 ||
                  run.constraints.length > 0) && <Divider />}
                {run.subject_area && (
                  <Body1>
                    <Body1Strong>Subject area: </Body1Strong>
                    {run.subject_area}
                  </Body1>
                )}
                {run.desired_outputs.length > 0 && (
                  <Body1>
                    <Body1Strong>Desired outputs: </Body1Strong>
                    {run.desired_outputs.join(", ")}
                  </Body1>
                )}
                {run.constraints.length > 0 && (
                  <Body1>
                    <Body1Strong>Constraints: </Body1Strong>
                    {run.constraints.join("; ")}
                  </Body1>
                )}
              </section>

              <section className={styles.card} aria-label="Scope">
                <Subtitle2>Data scope</Subtitle2>
                {run.scope ? (
                  <>
                    <Body1>
                      <Body1Strong>Workspace: </Body1Strong>
                      {run.scope.workspace_name ?? run.scope.workspace_id}
                    </Body1>
                    <Body1>
                      <Body1Strong>Lakehouse: </Body1Strong>
                      {run.scope.lakehouse_name}
                    </Body1>
                    <Body1>
                      <Body1Strong>Schemas: </Body1Strong>
                      {run.scope.schemas.length > 0 ? run.scope.schemas.join(", ") : "All"}
                    </Body1>
                    <Body1>
                      <Body1Strong>SQL endpoint: </Body1Strong>
                      <span className={styles.mono}>
                        {run.scope.sql_endpoint?.server ?? "Not provisioned yet"}
                      </span>
                    </Body1>
                  </>
                ) : (
                  <Body1>No data scope selected yet.</Body1>
                )}
                {run.warnings.map((warning) => (
                  <MessageBar key={warning} intent="warning">
                    <MessageBarBody>{warning}</MessageBarBody>
                  </MessageBar>
                ))}
              </section>
            </div>

            <section className={styles.card} aria-label="Timeline">
              <Subtitle2>State timeline</Subtitle2>
              <ol className={styles.timeline}>
                {[...run.history].reverse().map((entry, index) => (
                  <li key={`${entry.at}-${index}`} className={styles.entry}>
                    <Body1Strong>
                      {entry.from ? `${entry.from} → ${entry.to}` : entry.to}
                    </Body1Strong>
                    <div>
                      <Caption1 className={styles.label}>
                        {formatDateTime(entry.at)} · {entry.actor}
                      </Caption1>
                    </div>
                    <Body1>{entry.reason}</Body1>
                  </li>
                ))}
              </ol>
            </section>
          </div>
        </>
      )}
    </div>
  );
}
