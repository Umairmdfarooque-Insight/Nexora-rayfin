// Report review and approval (T-604): generate the report plan, publish the
// report, validate (Gate G5) and repair it, then review each page's render
// next to its visuals' binding and query status, with Approve / Reject as
// project_reviewer and business_owner and a Regenerate for one visual or
// page. Ported in spirit from rayfin-ui's src/app/runs/[id]/report/page.tsx
// to this workload's pages (T-104/T-604): the page no longer fetches render
// images with a bearer token (a UDF has none to give an <img>); it calls the
// `get_run_file` udf (`api.getRunFile`) and shows the base64 body instead.
import {
  Badge,
  Body1,
  Body1Strong,
  Button,
  Caption1,
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
  approveBlockedReason,
  canRegenerate,
  canRejectAs,
  checkColor,
  checkLabel,
  failedChecks,
  needsValidation,
  pageQuestions,
  pageTitle,
  regenerateRole,
  regenerateTarget,
  renderPlaceholder,
  REPORT_STALE_MESSAGE,
  roleLabel,
  roleScope,
  statusColor,
  statusLabel,
  targetLabel,
} from "../lib/report.ts";
import { isStale } from "../lib/review.ts";
import type { Me, ReportPageReview, ReportReview, ReportVisualReview, RunDetail } from "../lib/types.ts";

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
  actions: { display: "flex", columnGap: "8px", alignItems: "center", flexWrap: "wrap" },
  list: { listStyleType: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", rowGap: "10px" },
  item: { display: "flex", flexDirection: "column", rowGap: "4px", minWidth: 0 },
  row: { display: "flex", columnGap: "8px", alignItems: "baseline", flexWrap: "wrap" },
  label: { color: tokens.colorNeutralForeground3 },
  detail: { color: tokens.colorNeutralForeground3, overflowWrap: "anywhere" },
  pageBody: { display: "grid", gridTemplateColumns: "minmax(0, 3fr) minmax(0, 2fr)", gap: "16px", alignItems: "start" },
  image: { width: "100%", height: "auto", borderRadius: tokens.borderRadiusMedium, border: `1px solid ${tokens.colorNeutralStroke2}` },
  placeholder: {
    minHeight: "180px",
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    rowGap: "6px",
    textAlign: "center",
    padding: "16px",
    backgroundColor: tokens.colorNeutralBackground3,
    borderRadius: tokens.borderRadiusMedium,
    border: `1px dashed ${tokens.colorNeutralStroke1}`,
  },
  visual: {
    display: "flex",
    flexDirection: "column",
    rowGap: "4px",
    padding: "8px",
    borderRadius: tokens.borderRadiusMedium,
    backgroundColor: tokens.colorNeutralBackground2,
    minWidth: 0,
  },
  regen: { display: "flex", flexDirection: "column", rowGap: "6px", marginTop: "4px" },
});

/** A page render, fetched through `get_run_file` (base64) and shown as a data URL. A file
 * over the UDF response limit is `413`: the placeholder and the Power BI link take over. */
function PageImage({ page }: { page: ReportPageReview }) {
  const styles = useStyles();
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const url = page.image_url;

  useEffect(() => {
    let cancelled = false;
    setSrc(null);
    setFailed(false);
    if (url) {
      api
        .getRunFile(url)
        .then((file) => {
          if (!cancelled) setSrc(`data:${file.content_type};base64,${file.base64}`);
        })
        .catch(() => !cancelled && setFailed(true));
    }
    return () => {
      cancelled = true;
    };
  }, [url]);

  const placeholder = renderPlaceholder(page);
  if (placeholder || failed) {
    return (
      <div className={styles.placeholder} role="img" aria-label={`No render of ${pageTitle(page)}`}>
        <Body1Strong>{placeholder?.text ?? "Render unavailable — open in Power BI"}</Body1Strong>
        {placeholder?.detail && <Caption1 className={styles.detail}>{placeholder.detail}</Caption1>}
        {(placeholder?.href ?? page.open_url) && (
          <a href={placeholder?.href ?? page.open_url ?? undefined} target="_blank" rel="noreferrer">
            Open the report in Power BI
          </a>
        )}
      </div>
    );
  }
  if (!src) return <Spinner size="small" label="Loading the render…" />;
  // eslint-disable-next-line @next/next/no-img-element
  return <img className={styles.image} src={src} alt={`Rendered page ${pageTitle(page)}`} />;
}

export function ReportPage({ runId, onTrace }: { runId: string; onTrace?: (visualId: string) => void }) {
  const styles = useStyles();
  const [run, setRun] = useState<RunDetail | null>(null);
  const [me, setMe] = useState<Me | null>(null);
  const [review, setReview] = useState<ReportReview | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<{ title: string; error: ApiError } | null>(null);
  const [stale, setStale] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [comment, setComment] = useState("");
  const [regen, setRegen] = useState<{ page: ReportPageReview; visual?: ReportVisualReview } | null>(null);
  const [guidance, setGuidance] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setStale(false);
    try {
      const [loaded, who] = await Promise.all([api.getRun(runId), api.getMe()]);
      setRun(loaded);
      setMe(who);
      try {
        setReview(await api.getReportReview(runId));
      } catch (e) {
        if ((e as ApiError).status === 404) {
          setReview(null);
        } else {
          throw e;
        }
      }
    } catch (e) {
      setError({ title: "Couldn't load the report review.", error: e as ApiError });
    } finally {
      setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function act(label: string, call: () => Promise<unknown>, done?: string): Promise<boolean> {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      await call();
      await load();
      if (done) setNotice(done);
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
    void act(`${action === "ACCEPT" ? "approve" : "reject"} as ${roleLabel(role)}`, () =>
      api.approve(runId, {
        artifact: "Report",
        action,
        expected_hash: review.report_hash,
        role,
        ...(action === "REJECT" ? { comment: comment.trim() } : {}),
      }),
    ).then((ok) => {
      if (ok && action === "REJECT") setComment("");
    });
  }

  function regenerate() {
    if (!review || !regen) return;
    const role = regenerateRole(review, me);
    void act(
      "regenerate",
      () =>
        api.approve(runId, {
          artifact: "Report",
          action: "REGENERATE",
          expected_hash: review.report_hash,
          role,
          target: regenerateTarget(regen.page, regen.visual),
          comment: guidance.trim(),
        }),
      "Regenerated. Gate G5 ran again on the new version.",
    ).then((ok) => {
      if (ok) {
        setRegen(null);
        setGuidance("");
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
  const failed = review ? failedChecks(review.gate) : [];
  const mayRegenerate = canRegenerate(review, me);

  return (
    <div className={styles.page}>
      <div className={styles.titleRow}>
        <Title2 as="h2">Report review</Title2>
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
            {REPORT_STALE_MESSAGE} Nothing was changed.
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
      {loading && !run && <Spinner label="Loading report review…" />}

      {run && (
        <section className={styles.card} aria-label="Build the report">
          <Subtitle2>Build the report</Subtitle2>
          <Body1>Plan the report from the approved model, generate and publish it to the sandbox, then validate (Gate G5).</Body1>
          <div className={styles.actions}>
            <Button
              disabled={working || run.state !== "MODEL_APPROVED"}
              onClick={() => void act("plan the report", () => api.generateReportPlan(runId)).then((ok) => ok && setNotice("Report plan generated."))}
            >
              {review ? "Regenerate plan" : "Generate plan"}
            </Button>
            <Button
              disabled={working || (run.state !== "MODEL_APPROVED" && run.state !== "REPORT_GENERATING")}
              onClick={() => void act("generate the report", () => api.generateReport(runId)).then((ok) => ok && setNotice("Report generated and published."))}
            >
              {review ? "Regenerate and publish" : "Generate report"}
            </Button>
            <Button
              disabled={working || run.state !== "REPORT_GENERATING"}
              onClick={() => void act("validate the report", () => api.validateReport(runId), "Validation finished.")}
            >
              Validate
            </Button>
            <Button
              disabled={working || run.state !== "REPORT_FAILED"}
              onClick={() => void act("repair the report", () => api.repairReport(runId), "Repair finished.")}
            >
              Repair
            </Button>
          </div>
          {run.state === "REPORT_FAILED" && (
            <MessageBar intent="warning">
              <MessageBarBody>Validation failed. Run Repair, or fix the issue and Validate again.</MessageBarBody>
            </MessageBar>
          )}
        </section>
      )}

      {run && !review && !loading && (
        <section className={styles.card} aria-label="No report">
          <StateBadge state={run.state} />
          <Body1>The report hasn't been generated for this run yet.</Body1>
        </section>
      )}

      {run && review && (
        <>
          <section className={styles.card} aria-label="Summary">
            <div className={styles.titleRow}>
              <StateBadge state={run.state} />
              <Badge color={review.gate.passed ? "success" : "danger"}>
                {review.gate.passed ? "Gate G5 passed" : "Gate G5 not passing"}
              </Badge>
              <Caption1 className={styles.label}>Version {review.version}</Caption1>
              <span className={styles.mono}>{review.report_hash}</span>
              <Button
                size="small"
                onClick={() => void act("validate the report", () => api.validateReport(runId), "Validation finished.")}
                disabled={working || review.state !== "REPORT_GENERATING"}
              >
                Validate again
              </Button>
            </div>
            <Body1>
              {review.pages.length} pages, {review.pages.reduce((n, p) => n + p.visuals.length, 0)} visuals.
            </Body1>
            {needsValidation(review) && (
              <MessageBar intent="warning">
                <MessageBarBody>
                  The latest validation is of another version of the report. Validate it again before approving.
                </MessageBarBody>
              </MessageBar>
            )}
            {failed.length > 0 && (
              <MessageBar intent="error">
                <MessageBarBody>
                  <MessageBarTitle>Gate G5 failed.</MessageBarTitle>
                  {failed.join(", ")}
                </MessageBarBody>
              </MessageBar>
            )}
            {review.unanswered_questions.length > 0 && (
              <MessageBar intent="warning">
                <MessageBarBody>
                  <MessageBarTitle>Unanswered questions.</MessageBarTitle>
                  {review.unanswered_questions.map((u) => `${u.question_id}: ${u.reason}`).join("; ")}
                </MessageBarBody>
              </MessageBar>
            )}
          </section>

          <section className={styles.card} aria-label="Approvals">
            <Subtitle2>Approvals</Subtitle2>
            <Body1 className={styles.detail}>
              The project reviewer and the business owner each approve, on this version of the report. A change to the
              report invalidates the approvals given before it.
            </Body1>
            <ul className={styles.list}>
              {review.approvals_status.map((s) => {
                const blocked = approveBlockedReason(review, me, s.role);
                return (
                  <li key={s.role} className={styles.item}>
                    <div className={styles.row}>
                      <Body1Strong>{roleLabel(s.role)}</Body1Strong>
                      <Badge color={statusColor(s.status)}>{statusLabel(s)}</Badge>
                    </div>
                    <Caption1 className={styles.label}>{roleScope(s.role)}</Caption1>
                    <div className={styles.actions}>
                      <Button appearance="primary" disabled={working || blocked !== null} onClick={() => decide(s.role, "ACCEPT")}>
                        Approve as {roleLabel(s.role)}
                      </Button>
                      <Button
                        disabled={working || !canRejectAs(review, me, s.role) || comment.trim().length === 0}
                        onClick={() => decide(s.role, "REJECT")}
                      >
                        Reject as {roleLabel(s.role)}
                      </Button>
                    </div>
                    {blocked && <Caption1 className={styles.detail}>Approve is unavailable: {blocked}</Caption1>}
                  </li>
                );
              })}
            </ul>
            <Textarea
              aria-label="Comment"
              placeholder="A comment, required to reject"
              value={comment}
              onChange={(_, d) => setComment(d.value)}
              resize="vertical"
              disabled={working}
            />
          </section>

          {review.pages.map((p) => (
            <section key={p.name} className={styles.card} aria-label={`Page ${pageTitle(p)}`}>
              <div className={styles.row}>
                <Subtitle2>{pageTitle(p)}</Subtitle2>
                <Badge appearance="outline" color={checkColor(p.render_status)}>
                  Render: {checkLabel(p.render_status)}
                </Badge>
                {p.open_url && (
                  <a href={p.open_url} target="_blank" rel="noreferrer">
                    Open in Power BI
                  </a>
                )}
                <Button
                  size="small"
                  disabled={working || !mayRegenerate}
                  onClick={() => {
                    setRegen({ page: p });
                    setGuidance("");
                  }}
                >
                  Regenerate page
                </Button>
              </div>
              <div className={styles.pageBody}>
                <PageImage page={p} />
                <div className={styles.item}>
                  <Caption1 className={styles.label}>Questions this page answers</Caption1>
                  <ul className={styles.list}>
                    {pageQuestions(p).map((q) => (
                      <li key={q.id}>
                        <Body1>
                          <Body1Strong>{q.id}</Body1Strong> {q.text}
                        </Body1>
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
              {regen && regen.page.name === p.name && !regen.visual && (
                <RegenerateForm
                  title={`Regenerate page ${pageTitle(p)}`}
                  guidance={guidance}
                  onGuidance={setGuidance}
                  disabled={working}
                  onRun={regenerate}
                  onCancel={() => setRegen(null)}
                />
              )}
              <ul className={styles.list}>
                {p.visuals.map((v) => (
                  <li key={v.id} className={styles.visual}>
                    <div className={styles.row}>
                      <Body1Strong>{v.title}</Body1Strong>
                      <Caption1 className={styles.label}>
                        {v.id}
                        {v.type ? `, ${v.type}` : ""}
                      </Caption1>
                      <Badge color={checkColor(v.bind_status)}>Binding: {checkLabel(v.bind_status)}</Badge>
                      <Badge color={checkColor(v.query_status)}>Query: {checkLabel(v.query_status)}</Badge>
                      {onTrace && (
                        <Button size="small" onClick={() => onTrace(v.id)}>
                          Trace
                        </Button>
                      )}
                      <Button
                        size="small"
                        disabled={working || !mayRegenerate}
                        onClick={() => {
                          setRegen({ page: p, visual: v });
                          setGuidance("");
                        }}
                      >
                        Regenerate
                      </Button>
                    </div>
                    <Caption1 className={styles.detail}>
                      Answers: {v.questions.length === 0 ? "no question" : v.questions.map((q) => `${q.id} ${q.text}`).join("; ")}
                    </Caption1>
                    {v.messages.map((m) => (
                      <Caption1 key={m} className={styles.detail}>
                        {m}
                      </Caption1>
                    ))}
                    {regen && regen.page.name === p.name && regen.visual?.id === v.id && (
                      <RegenerateForm
                        title={`Regenerate ${v.id}: ${v.title}`}
                        guidance={guidance}
                        onGuidance={setGuidance}
                        disabled={working}
                        onRun={regenerate}
                        onCancel={() => setRegen(null)}
                      />
                    )}
                  </li>
                ))}
              </ul>
            </section>
          ))}

          <section className={styles.card} aria-label="History">
            <Subtitle2>Approval history</Subtitle2>
            {review.approvals_done.length === 0 && <Body1>No approvals yet.</Body1>}
            <ul className={styles.list}>
              {review.approvals_done.map((a) => (
                <li key={a.approval_id} className={styles.item}>
                  <div className={styles.row}>
                    <Badge color={a.action === "ACCEPT" ? "success" : a.action === "REJECT" ? "danger" : "informative"}>{a.action}</Badge>
                    <Body1Strong>{roleLabel(a.role ?? "")}</Body1Strong>
                    <Caption1 className={styles.label}>
                      {a.user.upn}, {formatDateTime(a.at)}, version {a.artifact_version}
                      {a.target ? `, ${targetLabel(a.target)}` : ""}
                    </Caption1>
                    {!a.current && <Badge color="warning">{a.note ?? "invalidated: report changed"}</Badge>}
                  </div>
                  {a.comment && <Caption1 className={styles.detail}>{a.comment}</Caption1>}
                </li>
              ))}
            </ul>
          </section>
        </>
      )}
    </div>
  );
}

function RegenerateForm(props: {
  title: string;
  guidance: string;
  onGuidance: (value: string) => void;
  disabled: boolean;
  onRun: () => void;
  onCancel: () => void;
}) {
  const styles = useStyles();
  return (
    <div className={styles.regen} role="group" aria-label={props.title}>
      <Body1Strong>{props.title}</Body1Strong>
      <Textarea
        aria-label="Guidance"
        placeholder="What should change? For example: use a column chart"
        value={props.guidance}
        onChange={(_, d) => props.onGuidance(d.value)}
        resize="vertical"
      />
      <Caption1 className={styles.detail}>
        Only this part of the report is rewritten. Gate G5 runs again, and the approvals given before it are invalidated.
      </Caption1>
      <div className={styles.actions}>
        <Button appearance="primary" disabled={props.disabled || props.guidance.trim().length === 0} onClick={props.onRun}>
          Regenerate
        </Button>
        <Button disabled={props.disabled} onClick={props.onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}
