"use client";

import {
  Badge,
  Body1,
  Body1Strong,
  Button,
  Caption1,
  Checkbox,
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
import { StateBadge } from "@/components/StateBadge";
import { api, type ApiError } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import {
  approveBlockedReason,
  canRejectAs,
  definitionLines,
  failedChecks,
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
} from "@/lib/model";
import { isStale } from "@/lib/review";
import type { Me, ModelReview, RunDetail } from "@/lib/types";

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
  actions: { display: "flex", columnGap: "8px", alignItems: "center", flexWrap: "wrap" },
  list: { listStyleType: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", rowGap: "10px" },
  item: { display: "flex", flexDirection: "column", rowGap: "4px", minWidth: 0 },
  row: { display: "flex", columnGap: "8px", alignItems: "baseline", flexWrap: "wrap" },
  label: { color: tokens.colorNeutralForeground3 },
  detail: { color: tokens.colorNeutralForeground3, overflowWrap: "anywhere" },
  code: {
    fontFamily: tokens.fontFamilyMonospace,
    fontSize: tokens.fontSizeBase200,
    whiteSpace: "pre-wrap",
    overflowWrap: "anywhere",
    backgroundColor: tokens.colorNeutralBackground3,
    padding: "8px",
    borderRadius: tokens.borderRadiusMedium,
    margin: 0,
  },
  side: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px", alignItems: "start" },
});

export default function ModelReviewPage() {
  const styles = useStyles();
  const { id } = useParams<{ id: string }>();
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
      const [loaded, who] = await Promise.all([api.getRun(id), api.getMe()]);
      setRun(loaded);
      setMe(who);
      setReview(await api.getModelReview(id).catch((e: ApiError) => (e.status === 404 ? null : Promise.reject(e))));
    } catch (e) {
      setError({ title: "Couldn't load the model review.", error: e as ApiError });
    } finally {
      setLoading(false);
    }
  }, [id]);

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
      api.approve(id, {
        artifact: "SemanticModel",
        action,
        expected_hash: review.model_hash,
        role,
        ...(action === "REJECT" ? { comment: comment.trim(), definition_wrong: isOwner(role) && definitionWrong } : {}),
      }),
    ).then((ok) => {
      if (ok && action === "REJECT") {
        setComment("");
        setDefinitionWrong(false);
      }
    });
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
  const failed = review ? failedChecks(review.gate) : [];
  const checks = review?.validation_report?.checks.filter((c) => c.status !== "pass") ?? [];

  return (
    <div className={styles.page}>
      <div className={styles.titleRow}>
        <Title2 as="h1">Model review</Title2>
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
      {loading && !run && <Spinner label="Loading model review…" />}

      {run && !review && !loading && (
        <section className={styles.card} aria-label="No model">
          <StateBadge state={run.state} />
          <Body1>The model hasn't been generated for this run yet.</Body1>
        </section>
      )}

      {run && review && (
        <>
          <section className={styles.card} aria-label="Summary">
            <div className={styles.titleRow}>
              <StateBadge state={run.state} />
              <Badge color={review.gate.passed ? "success" : "danger"}>
                {review.gate.passed ? "Gate G4 passed" : "Gate G4 not passing"}
              </Badge>
              <Caption1 className={styles.label}>Version {review.version}</Caption1>
              <span className={styles.mono}>{review.model_hash}</span>
              <Button
                size="small"
                onClick={() => void act("validate the model", () => api.validateModel(id), "Validation finished.")}
                disabled={working || review.state !== "MODEL_GENERATING"}
              >
                Validate again
              </Button>
            </div>
            <Body1>
              {review.tables.length} tables, {review.relationships.length} relationships, {review.measures.length} measures,{" "}
              {review.roles.length} RLS roles.
            </Body1>
            {needsValidation(review) && (
              <MessageBar intent="warning">
                <MessageBarBody>
                  The latest validation is of another version of the model. Validate it again before approving.
                </MessageBarBody>
              </MessageBar>
            )}
            {failed.length > 0 && (
              <MessageBar intent="error">
                <MessageBarBody>
                  <MessageBarTitle>Gate G4 failed.</MessageBarTitle>
                  {failed.join(", ")}
                </MessageBarBody>
              </MessageBar>
            )}
          </section>

          <section className={styles.card} aria-label="Approvals">
            <Subtitle2>Approvals</Subtitle2>
            <Body1 className={styles.detail}>
              Each role approves on its own, on this version of the model. A change to the model invalidates the approvals
              given before it.
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
            />
            <Checkbox
              label="The KPI definition itself is wrong (business owner only): send the run back to requirement review"
              checked={definitionWrong}
              onChange={(_, d) => setDefinitionWrong(!!d.checked)}
            />
            <Caption1 className={styles.detail}>
              A business owner can't redefine a KPI here. This sends the run to NEEDS_INPUT for requirement review and
              changes nothing in the contract.
            </Caption1>
          </section>

          <section className={styles.card} aria-label="Measures">
            <Subtitle2>Measures against their approved definitions</Subtitle2>
            {review.measures.length === 0 && <Body1>The model has no measures.</Body1>}
            <ul className={styles.list}>
              {review.measures.map((m) => (
                <li key={m.name} className={styles.item}>
                  <div className={styles.row}>
                    <Body1Strong>{m.name}</Body1Strong>
                    <Caption1 className={styles.label}>on {m.table}</Caption1>
                    <Badge color={reconciliationColor(m.reconciliation.status)}>{m.reconciliation.status.replace("_", " ")}</Badge>
                  </div>
                  <div className={styles.side}>
                    <div className={styles.item}>
                      <Caption1 className={styles.label}>DAX</Caption1>
                      <pre className={styles.code}>{m.dax ?? "No DAX yet"}</pre>
                    </div>
                    <div className={styles.item}>
                      <Caption1 className={styles.label}>Approved definition</Caption1>
                      {definitionLines(m).map((line) => (
                        <Body1 key={line}>{line}</Body1>
                      ))}
                    </div>
                  </div>
                  <div className={styles.row}>
                    {reconciliationRows(m.reconciliation).map((r) => (
                      <Caption1 key={r.label}>
                        <span className={styles.label}>{r.label}:</span> {r.value}
                      </Caption1>
                    ))}
                  </div>
                  {m.reconciliation.message && <Caption1 className={styles.detail}>{m.reconciliation.message}</Caption1>}
                </li>
              ))}
            </ul>
          </section>

          <div className={styles.columns}>
            <div className={styles.stack}>
              <section className={styles.card} aria-label="Tables">
                <Subtitle2>Tables</Subtitle2>
                <ul className={styles.list}>
                  {review.tables.map((t) => (
                    <li key={t.name} className={styles.item}>
                      <div className={styles.row}>
                        <Body1Strong>{t.name}</Body1Strong>
                        <Badge appearance="outline">{t.role}</Badge>
                        <Caption1 className={styles.label}>{t.source ?? "no source"}</Caption1>
                      </div>
                      <Caption1 className={styles.detail}>
                        {t.columns.filter((c) => !c.is_hidden).map((c) => c.name).join(", ") || "No visible columns"}
                        {t.hierarchies.length > 0 && ` · hierarchies: ${t.hierarchies.join(", ")}`}
                      </Caption1>
                    </li>
                  ))}
                </ul>
              </section>
              <section className={styles.card} aria-label="Row-level security">
                <Subtitle2>Row-level security</Subtitle2>
                {review.roles.length === 0 && <Body1>No roles: the model has no row-level security.</Body1>}
                <ul className={styles.list}>
                  {review.roles.map((r) => (
                    <li key={r.name} className={styles.item}>
                      <Body1Strong>{r.name}</Body1Strong>
                      <Body1>{r.description}</Body1>
                    </li>
                  ))}
                </ul>
              </section>
            </div>
            <div className={styles.stack}>
              <section className={styles.card} aria-label="Relationships">
                <Subtitle2>Relationships</Subtitle2>
                {review.relationships.length === 0 && <Body1>No relationships.</Body1>}
                <ul className={styles.list}>
                  {review.relationships.map((r) => (
                    <li key={r.name} className={styles.item}>
                      <Body1Strong>{r.name}</Body1Strong>
                      <Body1>
                        {r.from.table}.{r.from.column} → {r.to.table}.{r.to.column}
                      </Body1>
                      <Caption1 className={styles.detail}>
                        {r.cardinality.replace(/_/g, " ")}, {filterDirection(r.cross_filter)}
                        {r.evidence_ref ? ` · evidence ${r.evidence_ref}` : ""}
                        {r.override_rationale ? ` · override: ${r.override_rationale}` : ""}
                      </Caption1>
                    </li>
                  ))}
                </ul>
              </section>
              <section className={styles.card} aria-label="Validation">
                <Subtitle2>Validation</Subtitle2>
                {!review.validation_report && <Body1>The model hasn't been validated yet.</Body1>}
                {review.validation_report && (
                  <Caption1 className={styles.label}>
                    Attempt {review.validation_report.attempt}, {formatDateTime(review.validation_report.started_at)} —{" "}
                    {review.validation_report.passed ? "passed" : "failed"}, {review.validation_report.checks.length} checks
                  </Caption1>
                )}
                {checks.length === 0 && review.validation_report && <Body1>Every check passed.</Body1>}
                <ul className={styles.list}>
                  {checks.map((c) => (
                    <li key={`${c.id}-${c.object}-${c.fingerprint}`} className={styles.item}>
                      <div className={styles.row}>
                        <Badge color={c.status === "fail" ? "danger" : "warning"}>{c.status}</Badge>
                        <Body1Strong>{c.id}</Body1Strong>
                        <Caption1 className={styles.label}>{c.object}</Caption1>
                      </div>
                      <Caption1 className={styles.detail}>{c.message}</Caption1>
                    </li>
                  ))}
                </ul>
              </section>
            </div>
          </div>

          <section className={styles.card} aria-label="History">
            <Subtitle2>Approval history</Subtitle2>
            {review.approvals_done.length === 0 && <Body1>No approvals yet.</Body1>}
            <ul className={styles.list}>
              {review.approvals_done.map((a) => (
                <li key={a.approval_id} className={styles.item}>
                  <div className={styles.row}>
                    <Badge color={a.action === "ACCEPT" ? "success" : "danger"}>{a.action}</Badge>
                    <Body1Strong>{roleLabel(a.role ?? "")}</Body1Strong>
                    <Caption1 className={styles.label}>
                      {a.user.upn}, {formatDateTime(a.at)}, version {a.artifact_version}
                    </Caption1>
                    {!a.current && <Badge color="warning">{a.note ?? "invalidated: model changed"}</Badge>}
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
