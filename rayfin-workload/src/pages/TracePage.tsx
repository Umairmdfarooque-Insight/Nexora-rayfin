// Trace of one report visual (T-703 FR-002): visual -> measure -> DAX -> approved KPI -> source
// columns with their profile evidence -> original prompt. Ported from rayfin-ui's
// src/app/runs/[id]/trace/[visualId]/page.tsx: no Next.js router here, so `onBack`/`onOpenAudit` replace
// the `Link`s back to the report review and the audit trail (opened from a visual's "Trace" button on
// the Report tab, ReportPage.tsx).
import {
  Body1,
  Body1Strong,
  Caption1,
  makeStyles,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Spinner,
  Subtitle2,
  Title2,
  tokens,
} from "@fluentui/react-components";
import { useCallback, useEffect, useState } from "react";

import { ErrorBanner } from "../components/ErrorBanner.tsx";
import { type ApiError, api } from "../lib/api.ts";
import { definitionRows, evidenceLines, traceSteps } from "../lib/audit.ts";
import { formatDateTime } from "../lib/format.ts";
import type { TraceResult } from "../lib/types.ts";

const useStyles = makeStyles({
  page: { display: "flex", flexDirection: "column", rowGap: "12px" },
  titleRow: { display: "flex", alignItems: "center", columnGap: "12px", flexWrap: "wrap" },
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
  steps: { display: "flex", flexWrap: "wrap", columnGap: "8px", margin: 0, padding: 0, listStyleType: "none" },
  detail: { color: tokens.colorNeutralForeground3, overflowWrap: "anywhere" },
  code: {
    fontFamily: tokens.fontFamilyMonospace,
    backgroundColor: tokens.colorNeutralBackground3,
    borderRadius: tokens.borderRadiusMedium,
    padding: "8px",
    margin: 0,
    whiteSpace: "pre-wrap",
    overflowWrap: "anywhere",
  },
  list: { listStyleType: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", rowGap: "10px" },
  row: { display: "flex", columnGap: "8px", alignItems: "baseline", flexWrap: "wrap" },
  link: { background: "none", border: "none", padding: 0, color: tokens.colorBrandForegroundLink, cursor: "pointer", font: "inherit" },
});

export function TracePage({
  runId,
  visualId,
  onBack,
  onOpenAudit,
}: {
  runId: string;
  visualId: string;
  onBack: () => void;
  onOpenAudit: () => void;
}) {
  const styles = useStyles();
  const [trace, setTrace] = useState<TraceResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<{ title: string; error: ApiError } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setTrace(await api.getTrace(runId, visualId));
    } catch (e) {
      setError({ title: "Couldn't load the trace.", error: e as ApiError });
    } finally {
      setLoading(false);
    }
  }, [runId, visualId]);

  useEffect(() => {
    void load();
  }, [load]);

  if (error?.error.status === 404 && !trace) {
    return (
      <div className={styles.page}>
        <MessageBar intent="warning" role="alert">
          <MessageBarBody>{error.error.message || `Nothing to trace: ${visualId}`}</MessageBarBody>
        </MessageBar>
        <button type="button" className={styles.link} onClick={onBack}>
          Back to the report review
        </button>
      </div>
    );
  }

  return (
    <div className={styles.page}>
      <div className={styles.titleRow}>
        <Title2 as="h2">Trace of visual {visualId}</Title2>
        <button type="button" className={styles.link} onClick={onBack}>
          Back to the report review
        </button>
        <button type="button" className={styles.link} onClick={onOpenAudit}>
          Audit trail
        </button>
        {loading && <Spinner size="tiny" />}
      </div>

      {error && <ErrorBanner title={error.title} error={error.error} onRetry={() => void load()} disabled={loading} />}

      {trace && (
        <>
          <ol className={styles.steps} aria-label="Trace steps">
            {traceSteps(trace).map((s, i) => (
              <li key={s}>
                <Caption1>
                  {i > 0 ? "→ " : ""}
                  {s}
                </Caption1>
              </li>
            ))}
          </ol>

          {trace.gaps.length > 0 && (
            <MessageBar intent="warning" role="alert">
              <MessageBarBody>
                <MessageBarTitle>Gaps in the trace.</MessageBarTitle>
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {trace.gaps.map((g) => (
                    <li key={g}>{g}</li>
                  ))}
                </ul>
              </MessageBarBody>
            </MessageBar>
          )}

          <section className={styles.card} aria-label="Visual">
            <Subtitle2>Visual</Subtitle2>
            <Body1Strong>{trace.visual.title}</Body1Strong>
            <Caption1 className={styles.detail}>
              {trace.visual.id}, {trace.visual.type}, page {trace.visual.page}
            </Caption1>
            {trace.visual.questions.map((q) => (
              <Caption1 key={q.id} className={styles.detail}>
                Answers {q.id}: {q.text}
              </Caption1>
            ))}
          </section>

          {trace.measure && (
            <section className={styles.card} aria-label="Measure">
              <Subtitle2>Measure and DAX</Subtitle2>
              <Body1Strong>{trace.measure.name}</Body1Strong>
              <Caption1 className={styles.detail}>
                {trace.measure.model_version != null ? `Model version ${trace.measure.model_version}` : "No model version"}
                {trace.other_measures.length > 0 ? `; the visual also uses ${trace.other_measures.join(", ")}` : ""}
              </Caption1>
              <pre className={styles.code}>{trace.measure.dax ?? "(no DAX found)"}</pre>
            </section>
          )}

          {trace.kpi && (
            <section className={styles.card} aria-label="Approved KPI">
              <Subtitle2>Approved KPI</Subtitle2>
              {trace.kpi.definition && <Body1Strong>{trace.kpi.definition.name}</Body1Strong>}
              {definitionRows(trace.kpi).map(([label, value]) => (
                <Body1 key={label}>
                  <b>{label}:</b> {value}
                </Body1>
              ))}
              <Caption1 className={styles.detail}>
                Contract version {trace.kpi.contract_version ?? "?"}
                {trace.kpi.approved_by ? `, approved by ${trace.kpi.approved_by}` : ", no approval record"}
                {trace.kpi.approved_at ? ` on ${formatDateTime(trace.kpi.approved_at)}` : ""}
              </Caption1>
            </section>
          )}

          <section className={styles.card} aria-label="Sources">
            <Subtitle2>Source columns and evidence</Subtitle2>
            {trace.sources.length === 0 && <Body1>No source columns are recorded for this KPI.</Body1>}
            <ul className={styles.list}>
              {trace.sources.map((s) => (
                <li key={`${s.table}.${s.column}.${s.need}`}>
                  <div className={styles.row}>
                    <Body1Strong>
                      {s.table}.{s.column}
                    </Body1Strong>
                    <Caption1 className={styles.detail}>
                      {s.role}: {s.need}
                    </Caption1>
                  </div>
                  {s.profile_evidence ? (
                    <>
                      <Caption1 className={styles.detail}>
                        {evidenceLines(s.profile_evidence).join(" · ")} (profiled {formatDateTime(s.profile_evidence.profiled_at)})
                      </Caption1>
                      {s.profile_evidence.queries.map((q) => (
                        <pre key={q} className={styles.code}>
                          {q}
                        </pre>
                      ))}
                    </>
                  ) : (
                    <Caption1 className={styles.detail}>No profile evidence.</Caption1>
                  )}
                </li>
              ))}
            </ul>
          </section>

          <section className={styles.card} aria-label="Prompt">
            <Subtitle2>Original prompt</Subtitle2>
            <Body1>{trace.prompt}</Body1>
          </section>
        </>
      )}
    </div>
  );
}
