"use client";

import {
  Button,
  Checkbox,
  Dropdown,
  Field,
  Input,
  makeStyles,
  Option,
  Spinner,
  Textarea,
  Title2,
  tokens,
} from "@fluentui/react-components";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ErrorBanner } from "@/components/ErrorBanner";
import { api, type ApiError } from "@/lib/api";
import type { Lakehouse, Workspace } from "@/lib/types";

const OUTPUTS = ["semantic model", "report"];

const useStyles = makeStyles({
  page: { display: "flex", flexDirection: "column", rowGap: "16px", maxWidth: "760px" },
  form: {
    backgroundColor: tokens.colorNeutralBackground1,
    borderRadius: tokens.borderRadiusLarge,
    boxShadow: tokens.shadow4,
    padding: "16px 20px",
    display: "flex",
    flexDirection: "column",
    rowGap: "14px",
  },
  fieldset: { border: "none", margin: 0, padding: 0 },
  legend: { fontWeight: tokens.fontWeightSemibold, padding: 0, marginBottom: "4px" },
  checks: { display: "flex", columnGap: "16px" },
  actions: { display: "flex", alignItems: "center", columnGap: "12px" },
  dropdown: { minWidth: "unset", width: "100%" },
});

type Banner = {
  title: string;
  error: ApiError;
  retry?: () => void;
  runId?: string;
};

export default function NewRunPage() {
  const styles = useStyles();
  const router = useRouter();

  const [prompt, setPrompt] = useState("");
  const [subjectArea, setSubjectArea] = useState("");
  const [outputs, setOutputs] = useState<string[]>([]);
  const [constraints, setConstraints] = useState("");

  const [workspaces, setWorkspaces] = useState<Workspace[] | null>(null);
  const [workspaceId, setWorkspaceId] = useState("");
  const [lakehouses, setLakehouses] = useState<Lakehouse[] | null>(null);
  const [lakehouseId, setLakehouseId] = useState("");
  const [loadingWorkspaces, setLoadingWorkspaces] = useState(false);
  const [loadingLakehouses, setLoadingLakehouses] = useState(false);

  const [submitting, setSubmitting] = useState(false);
  const [createdRunId, setCreatedRunId] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [banner, setBanner] = useState<Banner | null>(null);

  const loadWorkspaces = useCallback(async () => {
    setLoadingWorkspaces(true);
    setBanner((current) => (current?.title.startsWith("Couldn't load workspaces") ? null : current));
    try {
      setWorkspaces(await api.listWorkspaces());
    } catch (e) {
      setBanner({
        title: "Couldn't load workspaces.",
        error: e as ApiError,
        retry: () => void loadWorkspaces(),
      });
    } finally {
      setLoadingWorkspaces(false);
    }
  }, []);

  useEffect(() => {
    void loadWorkspaces();
  }, [loadWorkspaces]);

  async function chooseWorkspace(id: string) {
    setWorkspaceId(id);
    setLakehouseId("");
    setLakehouses(null);
    setFieldErrors((current) => ({ ...current, workspace_id: "", lakehouse_id: "" }));
    setLoadingLakehouses(true);
    try {
      const list = await api.listLakehouses(id);
      setLakehouses(list);
      if (list.length === 1) {
        setLakehouseId(list[0].id);
      }
    } catch (e) {
      setBanner({
        title: "Couldn't load Lakehouses.",
        error: e as ApiError,
        retry: () => void chooseWorkspace(id),
      });
    } finally {
      setLoadingLakehouses(false);
    }
  }

  const noWorkspaces = workspaces !== null && workspaces.length === 0;
  const noLakehouses = lakehouses !== null && lakehouses.length === 0;
  const scopeIncomplete =
    workspaceId !== "" && (loadingLakehouses || noLakehouses || lakehouseId === "");
  const locked = createdRunId !== null; // the run exists; only the scope can be retried

  async function submit() {
    setBanner(null);
    setFieldErrors({});
    setSubmitting(true);
    let runId = createdRunId;
    try {
      if (!runId) {
        const run = await api.createRun({
          prompt,
          ...(subjectArea.trim() ? { subject_area: subjectArea.trim() } : {}),
          desired_outputs: outputs,
          constraints: constraints
            .split("\n")
            .map((line) => line.trim())
            .filter(Boolean),
        });
        runId = run.run_id;
        setCreatedRunId(runId);
      }
      if (workspaceId && lakehouseId) {
        await api.setScope(runId, { workspace_id: workspaceId, lakehouse_id: lakehouseId });
      }
      router.push(`/runs/${runId}`);
    } catch (e) {
      const error = e as ApiError;
      if (error.status === 422 && Object.keys(error.fields).length > 0) {
        setFieldErrors(error.fields);
      } else {
        setBanner({
          title: runId
            ? "The run was created, but its data scope couldn't be set."
            : "Couldn't create the run.",
          error,
          retry: () => void submit(),
          runId: runId ?? undefined,
        });
      }
      setSubmitting(false);
    }
  }

  function toggleOutput(name: string, checked: boolean) {
    setOutputs((current) =>
      checked ? [...current, name] : current.filter((item) => item !== name),
    );
  }

  const workspaceName = workspaces?.find((w) => w.id === workspaceId)?.display_name ?? "";
  const lakehouseName = lakehouses?.find((l) => l.id === lakehouseId)?.display_name ?? "";

  return (
    <div className={styles.page}>
      <Title2 as="h1">New run</Title2>

      {banner && (
        <ErrorBanner
          title={banner.title}
          error={banner.error}
          onRetry={banner.retry}
          disabled={submitting || loadingWorkspaces || loadingLakehouses}
        >
          {banner.runId && (
            <>
              {" "}
              <Link href={`/runs/${banner.runId}`}>Open the run without a scope</Link>
            </>
          )}
        </ErrorBanner>
      )}

      <form
        className={styles.form}
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <Field
          label="Business requirement"
          required
          hint="Describe what you want to see, in plain English."
          validationState={fieldErrors.prompt ? "error" : "none"}
          validationMessage={fieldErrors.prompt}
        >
          <Textarea
            value={prompt}
            onChange={(_, data) => setPrompt(data.value)}
            rows={5}
            resize="vertical"
            readOnly={locked}
          />
        </Field>

        <Field
          label="Subject area"
          validationState={fieldErrors.subject_area ? "error" : "none"}
          validationMessage={fieldErrors.subject_area}
        >
          <Input
            value={subjectArea}
            onChange={(_, data) => setSubjectArea(data.value)}
            readOnly={locked}
            placeholder="For example: Sales"
          />
        </Field>

        <fieldset className={styles.fieldset}>
          <legend className={styles.legend}>Desired outputs</legend>
          <div className={styles.checks}>
            {OUTPUTS.map((name) => (
              <Checkbox
                key={name}
                label={name}
                checked={outputs.includes(name)}
                disabled={locked}
                onChange={(_, data) => toggleOutput(name, data.checked === true)}
              />
            ))}
          </div>
        </fieldset>

        <Field
          label="Constraints"
          hint="One per line."
          validationState={fieldErrors.constraints ? "error" : "none"}
          validationMessage={fieldErrors.constraints}
        >
          <Textarea
            value={constraints}
            onChange={(_, data) => setConstraints(data.value)}
            rows={3}
            resize="vertical"
            readOnly={locked}
          />
        </Field>

        <Field
          label="Workspace"
          validationState={
            fieldErrors.workspace_id ? "error" : noWorkspaces ? "warning" : "none"
          }
          validationMessage={
            fieldErrors.workspace_id ||
            (noWorkspaces
              ? "You don't have access to any Fabric workspace"
              : undefined)
          }
          hint={loadingWorkspaces ? "Loading workspaces…" : undefined}
        >
          <Dropdown
            className={styles.dropdown}
            placeholder={workspaces === null ? "Workspaces unavailable" : "Select a workspace"}
            value={workspaceName}
            selectedOptions={workspaceId ? [workspaceId] : []}
            disabled={loadingWorkspaces || !workspaces?.length || submitting}
            onOptionSelect={(_, data) => {
              if (data.optionValue) void chooseWorkspace(data.optionValue);
            }}
          >
            {(workspaces ?? []).map((w) => (
              <Option key={w.id} value={w.id} text={w.display_name}>
                {w.display_name}
              </Option>
            ))}
          </Dropdown>
        </Field>

        <Field
          label="Lakehouse"
          validationState={
            fieldErrors.lakehouse_id ? "error" : noLakehouses ? "warning" : "none"
          }
          validationMessage={
            fieldErrors.lakehouse_id ||
            (noLakehouses ? "No Lakehouses in this workspace" : undefined)
          }
          hint={loadingLakehouses ? "Loading Lakehouses…" : undefined}
        >
          <Dropdown
            className={styles.dropdown}
            placeholder={workspaceId ? "Select a Lakehouse" : "Select a workspace first"}
            value={lakehouseName}
            selectedOptions={lakehouseId ? [lakehouseId] : []}
            disabled={!workspaceId || loadingLakehouses || noLakehouses || submitting}
            onOptionSelect={(_, data) => setLakehouseId(data.optionValue ?? "")}
          >
            {(lakehouses ?? []).map((l) => (
              <Option key={l.id} value={l.id} text={l.display_name}>
                {l.display_name}
              </Option>
            ))}
          </Dropdown>
        </Field>

        <div className={styles.actions}>
          <Button type="submit" appearance="primary" disabled={submitting || scopeIncomplete}>
            {locked ? "Set scope and open run" : "Create run"}
          </Button>
          {submitting && <Spinner size="tiny" label="Working…" labelPosition="after" />}
        </div>
      </form>
    </div>
  );
}
