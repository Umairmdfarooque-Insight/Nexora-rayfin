import { Body1, Caption1, MessageBar, MessageBarBody, MessageBarTitle, Spinner } from "@fluentui/react-components";
import { useEffect, useState } from "react";

import type { ActiveJob } from "../lib/types.ts";

function elapsedSeconds(startedAt: string): number {
  const started = new Date(startedAt).getTime();
  if (Number.isNaN(started)) return 0;
  return Math.max(0, Math.round((Date.now() - started) / 1000));
}

/** A job's progress (T-104 FR-4): "Queued" or "Running · <elapsed>"; the
 * caller disables other actions on the run while this is shown. */
export function JobProgress({ job }: { job: ActiveJob }) {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (job.status !== "running" && job.status !== "queued") return;
    const id = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [job.status]);

  if (job.status === "queued") {
    return (
      <MessageBar intent="info">
        <MessageBarBody>
          <MessageBarTitle>Queued</MessageBarTitle>
          Waiting for {job.operation} to start.
        </MessageBarBody>
      </MessageBar>
    );
  }
  if (job.status === "running") {
    return (
      <MessageBar intent="info">
        <MessageBarBody>
          <Spinner size="tiny" />
          <MessageBarTitle>Running · {elapsedSeconds(job.started_at)}s</MessageBarTitle>
          {job.operation}
        </MessageBarBody>
      </MessageBar>
    );
  }
  if (job.status === "failed") {
    return (
      <MessageBar intent="error">
        <MessageBarBody>
          <MessageBarTitle>The job failed</MessageBarTitle>
          {job.error?.error?.message ?? job.operation}
          {job.error?.error?.details?.job_instance_id ? (
            <Caption1> (job instance {String(job.error.error.details.job_instance_id)})</Caption1>
          ) : null}
        </MessageBarBody>
      </MessageBar>
    );
  }
  if (job.status === "cancelled") {
    return (
      <MessageBar intent="warning">
        <MessageBarBody>
          <MessageBarTitle>Cancelled</MessageBarTitle>
          <Body1>{job.operation} never started within the reservation window.</Body1>
        </MessageBarBody>
      </MessageBar>
    );
  }
  return null;
}
