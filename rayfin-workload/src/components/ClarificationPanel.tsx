// Run detail's Clarification panel (T-205 FR-1): lists open questions with
// inline answer and dismiss controls, and owns the Compile / Compile again
// action. Compile and answer are jobs (JobProgress on the run already shows
// their progress; this panel just disables itself while one is running).
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
  MessageBarBody,
  Spinner,
  Subtitle2,
  Textarea,
  tokens,
} from "@fluentui/react-components";
import { useState } from "react";

import { type ApiError, api } from "../lib/api.ts";
import { canChangeDraft, describeError } from "../lib/review.ts";
import type { RunDetail } from "../lib/types.ts";
import { ErrorBanner } from "./ErrorBanner.tsx";
import { LongText } from "./LongText.tsx";

const MAX_ANSWER = 2000;

const useStyles = makeStyles({
  panel: {
    backgroundColor: tokens.colorNeutralBackground1,
    borderRadius: tokens.borderRadiusLarge,
    boxShadow: tokens.shadow4,
    padding: "12px 16px",
    display: "flex",
    flexDirection: "column",
    rowGap: "12px",
  },
  header: { display: "flex", alignItems: "center", columnGap: "12px", flexWrap: "wrap" },
  grow: { flexGrow: 1 },
  question: {
    display: "flex",
    flexDirection: "column",
    rowGap: "8px",
    paddingLeft: "12px",
    borderLeft: `3px solid ${tokens.colorBrandStroke1}`,
  },
  blocking: { borderLeftColor: tokens.colorPaletteRedBorder2 },
  meta: { display: "flex", columnGap: "8px", alignItems: "center", flexWrap: "wrap" },
  actions: { display: "flex", columnGap: "8px", alignItems: "center", flexWrap: "wrap" },
  dismiss: { display: "flex", columnGap: "8px", alignItems: "flex-end", flexWrap: "wrap" },
});

interface Props {
  run: RunDetail;
  /** Called with the run the API returned after an action. */
  onRun: (run: RunDetail) => void;
  /** Switches to the Contract tab, when there is a contract to review. */
  onOpenContract?: () => void;
}

export function ClarificationPanel({ run, onRun, onOpenContract }: Props) {
  const styles = useStyles();
  const editable = canChangeDraft(run);
  // The typed text is kept per question until the API accepts it, so a network
  // error never loses it.
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<{ title: string; error: ApiError; retry: () => void } | null>(null);

  async function perform(key: string, title: string, run_: () => Promise<{ run: RunDetail }>, done?: () => void) {
    setBusy(key);
    setError(null);
    try {
      const result = await run_();
      done?.();
      onRun(result.run);
    } catch (e) {
      setError({ title, error: e as ApiError, retry: () => void perform(key, title, run_, done) });
    } finally {
      setBusy(null);
    }
  }

  const compile = () => perform("compile", "Couldn't compile the requirement.", () => api.compile(run.run_id));

  const answer = (qid: string) =>
    perform(
      `answer:${qid}`,
      "Couldn't send your answer. What you typed is kept.",
      () => api.answerQuestion(run.run_id, qid, (answers[qid] ?? "").trim()),
      () => setAnswers(({ [qid]: _removed, ...rest }) => rest),
    );

  const dismiss = (qid: string) =>
    perform(
      `dismiss:${qid}`,
      "Couldn't dismiss the question.",
      () => api.dismissQuestion(run.run_id, qid, (reasons[qid] ?? "").trim()),
      () => setReasons(({ [qid]: _removed, ...rest }) => rest),
    );

  const jobActive = run.active_job && ["queued", "running"].includes(run.active_job.status);
  const disabled = busy !== null || Boolean(jobActive);
  const compiling = busy === "compile" || (jobActive && run.active_job?.operation.includes("/compile"));
  const hasContract = run.contract !== null;

  return (
    <section className={styles.panel} aria-label="Clarification">
      <div className={styles.header}>
        <Subtitle2 className={styles.grow}>Clarification</Subtitle2>
        {hasContract && onOpenContract && (
          <Button appearance="transparent" onClick={onOpenContract}>
            Contract review
          </Button>
        )}
        {editable && (
          <Button appearance="primary" onClick={() => void compile()} disabled={disabled}>
            {hasContract ? "Compile again" : "Compile"}
          </Button>
        )}
      </div>

      {compiling && <Spinner label="Compiling the requirement. This can take up to a minute…" labelPosition="after" />}

      {error && (
        <ErrorBanner title={error.title} error={error.error} onRetry={error.retry} disabled={busy !== null}>
          {error.error.code === "STALE_VERSION" ? ` ${describeError(error.error)}` : null}
        </ErrorBanner>
      )}

      {!hasContract && !compiling && (
        <Body1>
          {editable
            ? "Compile the requirement to see the contract and any questions about it."
            : "No contract exists for this run."}
        </Body1>
      )}

      {hasContract && run.open_questions.length === 0 && (
        <MessageBar intent="success">
          <MessageBarBody>No open questions.</MessageBarBody>
        </MessageBar>
      )}

      {run.open_questions.map((question) => {
        const text = answers[question.id] ?? "";
        const reason = reasons[question.id] ?? "";
        const sending = busy === `answer:${question.id}`;
        const dismissing = busy === `dismiss:${question.id}`;
        return (
          <div key={question.id} className={`${styles.question} ${question.blocking ? styles.blocking : ""}`}>
            <div className={styles.meta}>
              <Body1Strong>{question.text}</Body1Strong>
              {question.blocking ? (
                <Badge color="danger" appearance="filled">
                  Blocking
                </Badge>
              ) : (
                <Badge appearance="outline">Optional</Badge>
              )}
              <Caption1>{question.id}</Caption1>
            </div>
            {question.reason && (
              <div>
                <LongText text={question.reason} />
              </div>
            )}
            {editable && (
              <>
                <Field
                  label="Your answer"
                  hint={`${text.length} / ${MAX_ANSWER}`}
                  validationState={text.length > MAX_ANSWER ? "error" : "none"}
                  validationMessage={text.length > MAX_ANSWER ? "The answer is too long." : undefined}
                >
                  <Textarea
                    value={text}
                    resize="vertical"
                    disabled={disabled}
                    onChange={(_, data) => setAnswers((current) => ({ ...current, [question.id]: data.value }))}
                  />
                </Field>
                <div className={styles.actions}>
                  <Button
                    appearance="primary"
                    onClick={() => void answer(question.id)}
                    disabled={disabled || !text.trim() || text.length > MAX_ANSWER}
                  >
                    Submit answer
                  </Button>
                  {sending && <Spinner size="tiny" label="Sending and recompiling…" labelPosition="after" />}
                </div>
                {!question.blocking && (
                  <div className={styles.dismiss}>
                    <Field label="Dismiss with a reason">
                      <Input
                        value={reason}
                        disabled={disabled}
                        onChange={(_, data) => setReasons((current) => ({ ...current, [question.id]: data.value }))}
                      />
                    </Field>
                    <Button onClick={() => void dismiss(question.id)} disabled={disabled || !reason.trim()}>
                      Dismiss
                    </Button>
                    {dismissing && <Spinner size="tiny" />}
                  </div>
                )}
              </>
            )}
          </div>
        );
      })}
    </section>
  );
}
