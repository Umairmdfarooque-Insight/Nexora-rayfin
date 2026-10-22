// The Rayfin home: the run list (T-104 FR-1). Router-agnostic: the host page
// passes a `navigate` callback, so this works both under the real Starter-Kit's
// own routing and in this project's local preview (App.tsx).
import {
  Body1,
  Button,
  Link as FluentLink,
  Spinner,
  Table,
  TableBody,
  TableCell,
  TableCellLayout,
  TableHeader,
  TableHeaderCell,
  TableRow,
  Title2,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { useCallback, useEffect, useState } from "react";

import { ErrorBanner } from "../components/ErrorBanner.tsx";
import { StateBadge } from "../components/StateBadge.tsx";
import { ApiError, api } from "../lib/api.ts";
import { formatDateTime } from "../lib/format.ts";
import type { RunSummary } from "../lib/types.ts";

const useStyles = makeStyles({
  page: { display: "flex", flexDirection: "column", rowGap: "16px" },
  titleRow: { display: "flex", alignItems: "center", justifyContent: "space-between" },
  card: {
    backgroundColor: tokens.colorNeutralBackground1,
    borderRadius: tokens.borderRadiusLarge,
    boxShadow: tokens.shadow4,
    padding: "8px 16px",
  },
  empty: {
    backgroundColor: tokens.colorNeutralBackground1,
    borderRadius: tokens.borderRadiusLarge,
    boxShadow: tokens.shadow4,
    padding: "48px 16px",
    textAlign: "center",
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    rowGap: "12px",
  },
  link: { fontFamily: tokens.fontFamilyMonospace },
});

export function RunsListPage({ navigate }: { navigate: (path: string) => void }) {
  const styles = useStyles();
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setRuns((await api.listRuns()).items);
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className={styles.page}>
      <div className={styles.titleRow}>
        <Title2 as="h1">Rayfin</Title2>
        <Button appearance="primary" onClick={() => navigate("/runs/new")}>
          New run
        </Button>
      </div>

      {error && <ErrorBanner title="Couldn't load runs." error={error} onRetry={load} disabled={loading} />}
      {loading && <Spinner label="Loading runs…" />}

      {!loading && !error && runs?.length === 0 && (
        <div className={styles.empty}>
          <Body1>No runs yet.</Body1>
          <FluentLink onClick={() => navigate("/runs/new")}>Create your first run</FluentLink>
        </div>
      )}

      {runs && runs.length > 0 && (
        <div className={styles.card}>
          <Table aria-label="Runs" size="small">
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Run</TableHeaderCell>
                <TableHeaderCell>State</TableHeaderCell>
                <TableHeaderCell>Created</TableHeaderCell>
                <TableHeaderCell>Prompt</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.map((run) => (
                <TableRow key={run.run_id}>
                  <TableCell>
                    <TableCellLayout>
                      <FluentLink className={styles.link} onClick={() => navigate(`/runs/${run.run_id}`)}>
                        {run.run_id.slice(0, 8)}
                      </FluentLink>
                    </TableCellLayout>
                  </TableCell>
                  <TableCell>
                    <StateBadge state={run.state} />
                  </TableCell>
                  <TableCell>{formatDateTime(run.created_at)}</TableCell>
                  <TableCell>{run.prompt_preview}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
