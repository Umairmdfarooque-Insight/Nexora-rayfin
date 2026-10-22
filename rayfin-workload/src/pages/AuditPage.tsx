// Audit trail (T-703 FR-003/FR-022): the timeline built from the run's tamper-evident event log.
// Ported from rayfin-ui's src/app/runs/[id]/audit/page.tsx to this workload's "Audit" tab of the
// "Nexora Run" item editor (NexoraRunItemEditor.tsx) -- no Next.js router here, just a runId prop.
import {
  Badge,
  Body1,
  Button,
  Caption1,
  makeStyles,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Spinner,
  Title2,
  tokens,
} from "@fluentui/react-components";
import { useCallback, useEffect, useState } from "react";

import { ErrorBanner } from "../components/ErrorBanner.tsx";
import { type ApiError, api } from "../lib/api.ts";
import {
  AUDIT_PAGE_SIZE,
  chainWarning,
  eventArtifact,
  eventTypeColor,
  eventTypeLabel,
  hasNext,
  hasPrevious,
  pageRange,
  previousOffset,
  shortHash,
} from "../lib/audit.ts";
import { formatDateTime } from "../lib/format.ts";
import type { AuditPage as AuditPageData } from "../lib/types.ts";

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
  list: { listStyleType: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", rowGap: "10px" },
  item: { display: "flex", flexDirection: "column", rowGap: "2px", minWidth: 0 },
  row: { display: "flex", columnGap: "8px", alignItems: "baseline", flexWrap: "wrap" },
  detail: { color: tokens.colorNeutralForeground3, overflowWrap: "anywhere" },
  mono: { fontFamily: tokens.fontFamilyMonospace, color: tokens.colorNeutralForeground3 },
  pager: { display: "flex", columnGap: "8px", alignItems: "center" },
});

export function AuditPage({ runId }: { runId: string }) {
  const styles = useStyles();
  const [page, setPage] = useState<AuditPageData | null>(null);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<{ title: string; error: ApiError } | null>(null);

  const load = useCallback(
    async (at: number) => {
      setLoading(true);
      setError(null);
      try {
        setPage(await api.getAudit(runId, at, AUDIT_PAGE_SIZE));
        setOffset(at);
      } catch (e) {
        setError({ title: "Couldn't load the audit trail.", error: e as ApiError });
      } finally {
        setLoading(false);
      }
    },
    [runId],
  );

  useEffect(() => {
    void load(0);
  }, [load]);

  if (error?.error.status === 404 && !page) {
    return (
      <div className={styles.page}>
        <MessageBar intent="warning" role="alert">
          <MessageBarBody>Run not found: {runId}</MessageBarBody>
        </MessageBar>
      </div>
    );
  }

  const warning = page ? chainWarning(page) : null;

  return (
    <div className={styles.page}>
      <div className={styles.titleRow}>
        <Title2 as="h2">Audit trail</Title2>
        <Button size="small" onClick={() => void load(offset)} disabled={loading}>
          Reload
        </Button>
        {loading && <Spinner size="tiny" />}
      </div>

      {error && <ErrorBanner title={error.title} error={error.error} onRetry={() => void load(offset)} disabled={loading} />}

      {page && (
        <>
          {warning ? (
            <MessageBar intent="error" role="alert">
              <MessageBarBody>
                <MessageBarTitle>Audit log tampered with.</MessageBarTitle>
                {warning}
              </MessageBarBody>
            </MessageBar>
          ) : (
            <Caption1 className={styles.detail}>Every event holds the hash of the one before it: the chain is intact.</Caption1>
          )}

          <section className={styles.card} aria-label="Timeline">
            <div className={styles.pager}>
              <Body1>{pageRange(offset, page.events.length, page.total)}</Body1>
              <Button size="small" disabled={loading || !hasPrevious(offset)} onClick={() => void load(previousOffset(offset))}>
                Earlier events
              </Button>
              <Button
                size="small"
                disabled={loading || !hasNext(offset, page.events.length, page.total)}
                onClick={() => void load(offset + AUDIT_PAGE_SIZE)}
              >
                Later events
              </Button>
            </div>
            <ul className={styles.list}>
              {page.events.map((e) => (
                <li key={e.seq} className={styles.item}>
                  <div className={styles.row}>
                    <Caption1 className={styles.mono}>#{e.seq}</Caption1>
                    {e.type && <Badge color={eventTypeColor(e.type)}>{eventTypeLabel(e.type)}</Badge>}
                    <Body1>{e.summary}</Body1>
                  </div>
                  {!e.unreadable && (
                    <Caption1 className={styles.detail}>
                      {e.at ? formatDateTime(e.at) : ""} · {e.actor}
                      {eventArtifact(e) ? ` · ${eventArtifact(e)}` : ""}
                      {e.hash ? (
                        <>
                          {" · "}
                          <span className={styles.mono} title={e.hash}>
                            {shortHash(e.hash)}
                          </span>
                        </>
                      ) : null}
                    </Caption1>
                  )}
                </li>
              ))}
            </ul>
          </section>
        </>
      )}
    </div>
  );
}
