"use client";

import {
  Body1,
  Button,
  makeStyles,
  Spinner,
  Table,
  TableBody,
  TableCell,
  TableCellLayout,
  TableHeader,
  TableHeaderCell,
  TableRow,
  Title2,
  tokens,
} from "@fluentui/react-components";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { ErrorBanner } from "@/components/ErrorBanner";
import { StateBadge } from "@/components/StateBadge";
import { api, type ApiError } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import type { RunSummary } from "@/lib/types";

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
  link: { color: tokens.colorBrandForegroundLink, fontFamily: tokens.fontFamilyMonospace },
});

export default function RunsPage() {
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
        <Title2 as="h1">Runs</Title2>
        <Button as="a" href="/runs/new" appearance="primary">
          New run
        </Button>
      </div>

      {error && (
        <ErrorBanner
          title="Couldn't load runs."
          error={error}
          onRetry={load}
          disabled={loading}
        />
      )}
      {loading && <Spinner label="Loading runs…" />}

      {!loading && !error && runs?.length === 0 && (
        <div className={styles.empty}>
          <Body1>No runs yet.</Body1>
          <Link href="/runs/new">Create your first run</Link>
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
                      <Link href={`/runs/${run.run_id}`} className={styles.link}>
                        {run.run_id.slice(0, 8)}
                      </Link>
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
