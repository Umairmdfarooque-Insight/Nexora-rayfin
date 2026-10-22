# Shared Contracts

These are data shapes and conventions used by more than one task. Tasks reference them as `CONTRACTS.md#<section>` instead of repeating them. If a task changes a shared shape, update this file in the same branch.

Conventions:
- Timestamps are ISO 8601 in UTC (`2026-10-14T09:30:00Z`).
- IDs are UUID v4 strings unless stated otherwise.
- JSON keys use `snake_case`.
- Every persisted artifact has `schema_version` (integer, starting at `1`).
- **Hash** means SHA-256 of the artifact's canonical form: JSON with sorted keys and no insignificant whitespace, or YAML dumped with sorted keys. It is written as `sha256:<hex>`.

---

**Fabric hosting.** Nexora is hosted on Microsoft Fabric ([fabric-hosting.md](../technical-documentation/fabric-hosting.md)).
- **Paths.** A path written `runs/<run_id>/…` in this file and in the tasks means the run folder. In Fabric that is `Files/runs/<run_id>/…` of the control Lakehouse `nexora_control`, reached through the `ArtifactStore`; locally it is `runs/<run_id>/…`. The manifest itself is a row of `dbo.nexora_runs` in Fabric (`SqlRunStore`) and `RunManifest.json` locally.
- **Endpoints.** A path such as `POST /runs/{id}/compile` is a **logical operation name**. [Fabric operations](#fabric-operations) says whether it runs as a User Data Function or a notebook job.

---

## Run

Persisted as `runs/<run_id>/RunManifest.json` (the spec's `RunManifest.json`, §10); in Fabric as the `manifest` column of `dbo.nexora_runs`.

```json
{
  "schema_version": 1,
  "run_id": "6f1c…",
  "prompt": "Show Net Revenue by month for 2024",
  "subject_area": "Sales",
  "desired_outputs": ["semantic model", "report"],
  "constraints": ["Use conformed tables only"],
  "state": "CREATED",
  "resume_state": null,
  "scope": {
    "workspace_id": "…", "workspace_name": "Sandbox",
    "lakehouse_id": "…", "lakehouse_name": "SalesLakehouse",
    "schemas": ["saleslt"],
    "sql_endpoint": { "server": "….datawarehouse.fabric.microsoft.com", "database": "SalesLakehouse" }
  },
  "created_by": { "oid": "…", "upn": "user@contoso.com", "name": "User Name" },
  "created_at": "…",
  "updated_at": "…",
  "history": [
    { "from": null, "to": "CREATED", "at": "…", "reason": "Run created", "actor": "user@contoso.com" }
  ],
  "artifacts": {
    "BusinessContract": { "path": "BusinessContract.yaml", "version": 2, "hash": "sha256:…", "status": "approved", "based_on": {}, "updated_at": "…" }
  },
  "deployment": null,
  "active_job": null
}
```

- `active_job` is set while a job operation runs (T-102): `{"job_id", "operation", "job_instance_id", "status": "queued"|"running"|"succeeded"|"failed"|"cancelled", "started_at", "finished_at", "error"}`. `error` holds the operation's error envelope when it failed. The last finished job stays until the next one starts. Only one job runs per run at a time.
- `scope` and `created_by` are `null` until T-103 and T-105 set them.
- `scope` also carries `workspace_name`, `lakehouse_name` and `sql_endpoint` (`null` when the Lakehouse has none yet), copied from Fabric when T-103 binds the scope. Connectors need them: the OneLake table API addresses a Lakehouse by name, and the SQL connector needs the endpoint. An empty `schemas` list means no restriction.
- `resume_state` is set only while the state is `NEEDS_INPUT`. It records the state to return to.
- Artifact `status` is one of `draft` | `approved` | `failed` | `stale` | `needs_input` | `data_gap`.
- `based_on` maps the name of each upstream artifact to the hash it was built from. T-701 uses it.

## Run states and transitions

These are the states from spec §11. Only these transitions are allowed. Anything else returns `ILLEGAL_TRANSITION`.

| From | Allowed to |
|---|---|
| `CREATED` | `NEEDS_CLARIFICATION`, `REQUIREMENT_APPROVED` |
| `NEEDS_CLARIFICATION` | `CREATED` (no blocking question left; draft ready for G1) |
| `REQUIREMENT_APPROVED` | `DATA_PROFILED`, `CREATED` (returned to requirement analysis) |
| `DATA_PROFILED` | `PLAN_APPROVED`, `REQUIREMENT_APPROVED` (re-harvest), `CREATED` |
| `PLAN_APPROVED` | `MODEL_GENERATING`, `DATA_PROFILED` (plan reopened), `CREATED` |
| `MODEL_GENERATING` | `MODEL_FAILED`, `MODEL_APPROVED` |
| `MODEL_FAILED` | `MODEL_GENERATING` (repair) |
| `MODEL_APPROVED` | `REPORT_GENERATING`, `CREATED`, `PLAN_APPROVED`, `DATA_PROFILED` (returned to planning, T-701) |
| `REPORT_GENERATING` | `REPORT_FAILED`, `REPORT_APPROVED` |
| `REPORT_FAILED` | `REPORT_GENERATING` (repair), `MODEL_GENERATING` (model defect) |
| `REPORT_APPROVED` | `DEPLOYING`, `CREATED`, `PLAN_APPROVED`, `DATA_PROFILED`, `MODEL_GENERATING` |
| `DEPLOYING` | `COMPLETED`, `ROLLED_BACK`, `NEEDS_INPUT` |
| `ROLLED_BACK` | `DEPLOYING` |
| `COMPLETED` | — (terminal) |
| any state except `COMPLETED` | `NEEDS_INPUT` |
| `NEEDS_INPUT` | the stored `resume_state`, or `CREATED` |

`DEPLOYING` is refused (`409 STALE_ARTIFACTS`, before the legality check) while any artifact has status `stale` (T-701).

`CREATED` doubles as "requirement draft without blocking questions". The spec has no separate state for it.

## Audit event (T-703)

`runs/<run_id>/events.jsonl`, append-only, one event per line: `{"seq", "at", "type", "actor", "artifact", "version", "hash", "summary", "key", "prev_event_hash"}`. `type` is `state` | `approval` | `artifact` | `gate` | `validation` | `deployment` | `return`. `seq` counts from 1. `prev_event_hash` is the SHA-256 of the canonical JSON of the previous line's event (sorted keys, no spaces), or `sha256:` plus 64 zeros for the first. `key` names what the event came from, so it is logged once. `GET /runs/{id}/audit` adds `first_broken_seq` to the spec's `{"events", "total", "chain_valid"}`, and the trace response adds `other_measures` and `gaps`.

## Deployment record (T-702)

`RunManifest.deployment`, set when a deployment starts: `deployment_id`, `target_workspace_id`, `started_at`, `finished_at`, `items` (`type`, `item_id`, `hash` = the approved artifact hash, `operation`), `backup_path` (`deploy/backup-<ts>/`, relative to the run folder), `smoke_tests` (`id`, `status` `pass`/`warning`/`fail`, `message`), `outcome` (`completed` | `rolled_back` | `rollback_failed`), and on a failure `error` and `rollback` (`item`, `type`, `action`, `status`). The final release approval is an approval record with artifact `Release`, role `release_approver` and the hash of the approved model and report hashes together.

## Return log and impact (T-701)

`runs/<run_id>/returns.jsonl` is append-only, one JSON object per line. A `returned` event records a return to `REQUIREMENT_ANALYSIS` (state `CREATED`), `PLANNING` (`DATA_PROFILED`) or `MODEL` (`PLAN_APPROVED`):

```json
{ "event": "returned", "return_id": "…", "run_id": "…", "at": "…", "user": { "oid": "…", "upn": "user@contoso.com" },
  "to": "REQUIREMENT_ANALYSIS", "from_state": "REPORT_APPROVED", "new_state": "CREATED", "reason": "Exclude tax",
  "baseline": { "BusinessContract": { "version": 2, "hash": "sha256:…", "path": "BusinessContract.yaml", "status": "approved" } },
  "unlocked": ["BusinessContract"], "approvals_before": 7 }
```

- `baseline` is the version and hash of every artifact when the run was first returned; a second return while one is open keeps the first baseline and `from_state`. Earlier versions stay on disk, so the baseline can be read back.
- `approvals_before` is the number of records `approvals.jsonl` held: model and report approvals before that position no longer count (a return reopens them even if the files come out identical).
- A `resolved` event (`event`, `return_id`, `at`, `reason`) closes the return once nothing is stale and the run is approved again as far as `from_state`.

`runs/<run_id>/Impact.json` holds the impact of the open return: `changed` (`artifact` and `objects`), `stale` (`artifact`, `objects`, `reason`, `removals`), `prior_status` (the status an artifact had before it was marked `stale`) and `fresh` (artifacts checked again that came out unchanged, by hash). Object names: `kpi:<name>`, `entity:<name>`, `measure:<name>`, `table:<name>`, `relationship:<name>`, `roles`, `file:<path in the TMDL folder>`, `visual:<page name>/<visual id>` in the ReportPlan and `visual:p<n>/<visual id>` in the report. `*` means the whole artifact. `removals` are the objects marked for removal (a deleted KPI's measure and visuals): shown for review, never dropped silently.

## Stage graphs

Orchestration is **hybrid**, with two layers:

| Layer | Built with | Owns |
|---|---|---|
| Run layer | The state machine and `RunStore` (T-101) | Run state, the transitions above, approvals, `history` and the audit trail. Waiting for a person (clarification, approval, `NEEDS_INPUT`) is always a run state. |
| Stage layer | A LangGraph `StateGraph` per stage | The LLM loop inside one stage call: propose, validate, retry or repair, then return a result |

A stage service (called from an API endpoint) checks the run state, invokes the stage graph, runs the gate on the result, and then makes the transition through `RunStore`.

Rules for every stage graph:
1. **No run-state writes.** A graph never calls `RunStore.transition` or `RunStore.save`, and never writes `RunManifest.json`. Nodes may write new versioned artifact files and append to `ValidationReport.json`. The stage service registers the artifact in the manifest after the graph returns.
2. **Deterministic routing.** Conditional edges route only on validator output (pass or fail, error codes, fingerprints, attempt counts), never on free LLM text.
3. **Bounded loops.** Every cycle has an `attempt` counter in the graph state, checked by a conditional edge against the task's limit. `run_graph` also passes a `recursion_limit`. A `GraphRecursionError` is a bug and returns `500 INTERNAL_ERROR`.
4. **Short-lived.** A graph finishes within the call that started it. No `interrupt()` and no persistent checkpointer. Graphs are compiled without a checkpointer. After a person responds, the next API call starts a new graph invocation from the run's artifacts.
5. **Shared LLM client.** LLM nodes call `app/llm/client.py`, so the T-705 payload guard and T-704 metrics apply. Don't use LangChain chat-model classes. The only new dependency is `langgraph`.
6. **No external tracing.** LangSmith tracing (`LANGSMITH_TRACING`, `LANGCHAIN_TRACING_V2`) stays off, because prompts and metadata must not leave the tenant (NFR-008). Tracing goes through T-704.
7. **Testable.** The LLM call is passed into `build_<name>_graph(llm=…)`, so a test can swap in a fake and check the node path without Azure.
8. **Jobs only.** A stage graph runs inside a notebook job (`job:` in [Fabric operations](#fabric-operations)), never inside a User Data Function. "Finishes within the call" (rule 4) means within the job.

Layout:
- `app/graphs/base.py`: `run_graph(graph, state: dict, *, run_id: str, stage: str, recursion_limit: int = 25) -> dict`. It invokes the graph with `config={"recursion_limit": …, "metadata": {"run_id": …, "stage": …}}`, records the node path, and converts `GraphRecursionError` to `INTERNAL_ERROR`. T-202 adds it.
- `app/graphs/<name>.py`: `build_<name>_graph(llm=…) -> CompiledStateGraph`, with the graph state as a `TypedDict`.

| Graph | Task | Nodes and loop | Bound |
|---|---|---|---|
| `compile` | T-202, reused by T-203 | `llm_compile → validate_schema → (retry llm_compile with errors) → apply_critical_terms → apply_locked_facts` | 2 LLM attempts |
| `dax_measure` | T-503 | `propose_dax → precheck_references → (retry propose_dax with errors)`, one invocation per measure | 2 attempts per measure |
| `repair` | T-505, reused by T-603 | `classify → deterministic_fix → llm_fix → scope_guard → validate → check_progress → (loop to classify, or stop)` | `max_attempts` from `config/repair_policy.yaml`. Also stops when there's no progress or only `user_input` errors remain |

Use plain functions, not a graph, for stages with no loop (for example T-301, T-302 and T-303).

## Fabric operations

Every logical operation the tasks name (`POST /runs/{id}/compile` and so on) keeps its name, request body, response body, status codes and error envelope. Only the transport changes. The hosting rule is in [fabric-hosting.md](../technical-documentation/fabric-hosting.md#operation-hosting-rule). T-102 builds the transport.

**Dispatcher.** `app/fabric/dispatch.py` holds the one table of operations. It maps `(method, path pattern)` to the existing stage-service function and its host (`udf` or `job`). It runs the run authorization (T-705) and converts exceptions to the error envelope through the same mapping as `app/api/errors.py`. UDF and job handlers call only the dispatcher, so they hold no business logic.

**UDF transport.** The `nexora_api` item exposes:
- `call(method: str, path: str, query: dict = {}, body: dict | None = None) -> {"status": int, "body": …}`. `body` is the old response body or the error envelope.
- `me() -> {oid, upn, name, roles}`.

The caller comes from the function's invocation context, never from the request.

**Job transport.**
1. The UI calls `call("POST", "/runs/{id}/jobs", body={"operation": "POST /runs/{id}/compile", "body": {...}})`. The dispatcher checks the run exists, the user may act on it, no job is active (`409 RUN_BUSY`) and the operation's cheap preconditions (state, required artifacts). It then reserves `active_job` (`status: queued`) and returns `202 {"job_id", "run": RunDetail}`.
2. The UI starts `nexora_stage_runner` through the Fabric Job Scheduler API (`POST /v1/workspaces/{home}/items/{notebook}/jobs/instances?jobType=RunNotebook`, parameters `job_id`, `run_id`, `operation`, `payload` as JSON) with the user's token. The job then runs as that user. It records `job_instance_id`, sets `running`, calls the dispatcher, writes the result to `runs/<id>/jobs/<job_id>.json` (`{"status", "body"}`, exactly what the old endpoint returned) and sets `succeeded` or `failed`.
3. The UI polls `GET /runs/{id}` (`active_job`) and then reads `GET /runs/{id}/jobs/{job_id}`.

A reservation that never got a `job_instance_id` within 10 minutes is released (`cancelled`). A job that ends without writing a result is `failed` with `502 JOB_FAILED`. If T-102 finds that a UDF can start a job as the user, steps 1 and 2 merge into one UDF call, and the contract the UI sees stays the same.

| Operation | Host | Task |
|---|---|---|
| `GET /health` | udf | T-102 |
| `GET /me` | udf (`me`) | T-204 |
| `POST /runs`, `GET /runs`, `GET /runs/{id}`, `GET /runs/{id}/artifacts` | udf | T-102 |
| `POST /runs/{id}/jobs`, `GET /runs/{id}/jobs/{job_id}` | udf | T-102 |
| `GET /fabric/workspaces`, `GET /fabric/workspaces/{id}/lakehouses` | ui (Fabric REST with the user's token, pickers only) | T-103 |
| `PUT /runs/{id}/scope` | job | T-103 |
| `POST /runs/{id}/compile` | job | T-202 |
| `POST /runs/{id}/questions/{qid}/answer` | job (it recompiles) | T-203 |
| `POST /runs/{id}/questions/{qid}/dismiss`, `PUT /runs/{id}/contract/kpis/{kpi_name}` | udf | T-203 |
| `POST /runs/{id}/approvals` | udf; **job** when `action` is `REGENERATE` | T-204, T-304, T-403, T-405, T-507, T-604, T-702 |
| `GET /runs/{id}/approvals`, `POST /runs/{id}/gates/G1/check`, `POST /runs/{id}/gates/G2/check`, `POST /runs/{id}/gates/G3/check` | udf | T-204, T-304, T-405 |
| `POST /runs/{id}/harvest`, `POST /runs/{id}/profile`, `POST /runs/{id}/relationships/analyze` | job | T-301, T-302, T-303 |
| `GET /runs/{id}/inventory`, `GET /runs/{id}/relationships` | udf | T-301, T-303 |
| `POST /runs/{id}/exceptions`, `POST /runs/{id}/data-gaps` | udf | T-304 |
| `POST /runs/{id}/plan/sources`, `POST /runs/{id}/plan/feasibility`, `POST /runs/{id}/plan/bridges` | job | T-401, T-402, T-403 |
| `POST /runs/{id}/plan` | udf (deterministic, control files only) | T-404 |
| `GET /runs/{id}/plan`, `GET /runs/{id}/plan/sources`, `PATCH /runs/{id}/plan` | udf | T-404, T-405 |
| `POST /runs/{id}/model/generate` | udf (deterministic, control files only) | T-502 |
| `POST /runs/{id}/model/measures`, `POST /runs/{id}/model/validate`, `POST /runs/{id}/model/repair`, `POST /runs/{id}/model/bridges/{bridge_id}/generate` | job | T-503, T-504, T-505, T-506 |
| `GET /runs/{id}/model/review` | udf | T-507 |
| `POST /runs/{id}/report/plan`, `POST /runs/{id}/report/generate`, `POST /runs/{id}/report/validate`, `POST /runs/{id}/report/repair` | job | T-601, T-602, T-603 |
| `GET /runs/{id}/report/plan`, `GET /runs/{id}/report/review` | udf | T-601, T-604 |
| `GET /runs/{id}/files/{path}` | udf (`get_run_file`: base64 body, same path rules) | T-604 |
| `POST /runs/{id}/return`, `GET /runs/{id}/impact` | udf | T-701 |
| `POST /runs/{id}/regenerate-stale` | job | T-701 |
| `POST /runs/{id}/deploy` | job (the old `202` plus background task becomes the job itself) | T-702 |
| `GET /runs/{id}/audit`, `GET /runs/{id}/trace` | udf | T-703 |
| `POST /runs/{id}/check_access` | job (T-705 access cache) | T-705 |

The CLI (`python -m app.cli …`) still calls the store directly. It is a development and admin tool, not a Fabric operation.

## Error envelope

Every API error uses this body:

```json
{ "error": { "code": "ILLEGAL_TRANSITION", "message": "CREATED → DEPLOYING is not allowed", "details": { "from": "CREATED", "to": "DEPLOYING" } } }
```

| HTTP | Code | Used for |
|---|---|---|
| 400 | `BAD_REQUEST` | Malformed request that isn't a validation error |
| 401 | `UNAUTHENTICATED`, `TOKEN_EXPIRED` | Missing or invalid token |
| 403 | `FORBIDDEN`, `CONSENT_REQUIRED`, `TARGET_NOT_ALLOWED`, `ACCESS_CHECK_REQUIRED` | Authenticated but not allowed (`ACCESS_CHECK_REQUIRED`: the user isn't the run's creator and has no fresh access decision, so the UI starts `job:check_access` and retries, T-705) |
| 404 | `RUN_NOT_FOUND`, `NOT_FOUND`, `SCHEMA_NOT_FOUND` | Unknown resource |
| 409 | `ILLEGAL_TRANSITION`, `WRONG_STATE`, `GATE_FAILED`, `CONTRACT_LOCKED`, `STALE_VERSION`, `SCOPE_LOCKED`, `QUESTION_CONFLICT`, `ALREADY_APPROVED`, `HASH_MISMATCH`, `MAPPING_RULES_REQUIRED`, `NO_FEASIBLE_KPIS`, `NAME_CONFLICT`, `BRIDGE_NOT_APPROVED`, `STALE_ARTIFACTS`, `SCOPE_VIOLATION`, `TARGET_BUSY`, `RUN_BUSY` | Conflicts with the current state (`RUN_BUSY`: a job is already queued or running for this run, T-102) || 422 | `VALIDATION_ERROR`, `PROMPT_REJECTED`, `PAYLOAD_REJECTED` | Invalid input, or an LLM payload blocked by the guard (T-705) |
| 429 | `RATE_LIMITED` | Upstream throttling the caller should retry |
| 502 | `UPSTREAM_ERROR`, `LLM_INVALID_OUTPUT`, `JOB_FAILED` | Fabric, Power BI or the LLM returned an error or unusable output; `JOB_FAILED`: the notebook job ended without writing a result (crash, cancel, capacity), `details.job_instance_id` |
| 504 | `UPSTREAM_TIMEOUT` | An upstream call timed out |
| 500 | `INTERNAL_ERROR`, `CONFIG_ERROR` | A bug or missing configuration. The message must not include secrets. |

## Roles

Taken from spec §2.2. During development they are mapped in `config/roles.yaml`, which T-204 introduces. In Fabric the file is read from `Files/config/roles.yaml` of the control Lakehouse (published from the repo by T-005):

```yaml
roles:
  business_owner:    [user@contoso.com]
  bi_engineer:       [user@contoso.com]
  security_reviewer: [user@contoso.com]
  project_reviewer:  [user@contoso.com]
  data_engineer:     [user@contoso.com]
  release_approver:  [user@contoso.com]
```

## Approval record

Appended to `runs/<run_id>/approvals.jsonl`, one JSON object per line. The file is append-only.

```json
{
  "approval_id": "…",
  "run_id": "…",
  "artifact": "BusinessContract",
  "artifact_version": 2,
  "artifact_hash": "sha256:…",
  "target": null,
  "action": "ACCEPT",
  "role": "business_owner",
  "user": { "oid": "…", "upn": "user@contoso.com" },
  "at": "…",
  "comment": "Looks right",
  "gate_result_ref": "gates/G1-…json"
}
```

- `action` is one of `ACCEPT` | `EDIT` | `REJECT` | `EXPLAIN` | `REGENERATE` (FR-018).
- `artifact_version` and `artifact_hash` are those of the artifact version the action acted on. `ACCEPT` and `EDIT` record the version they produced (for `ACCEPT`, the locked, `approved` version).
- `role` is the role the user held when acting (`business_owner` first), or `null` if the user holds none. `gate_result_ref` is set on `ACCEPT`. For the `SemanticModel` (T-507) the request names the role the user acts as (`role`), the user must hold it, and each role is its own action, so `role` is exactly that role.
- A refused action (`FORBIDDEN`, `GATE_FAILED`, `STALE_VERSION`, …) writes no record.
- `target` optionally narrows the action to one object, for example `{ "page": "Overview", "visual": "v3" }`.

## Gate result

Saved as `runs/<run_id>/gates/<gate>-<timestamp>.json`.

```json
{
  "gate": "G1",
  "artifact": "BusinessContract",
  "artifact_hash": "sha256:…",
  "passed": false,
  "checked_at": "…",
  "checks": [
    { "id": "G1.2", "description": "No blocking question open", "status": "fail", "evidence": { "open_blocking": ["Q1"] } }
  ]
}
```

A `409 GATE_FAILED` carries `details.failed_checks` (the ids) and `details.gate_result`. Check `status` is `pass` | `fail` | `warning`. A gate passes only when no check has the status `fail`.

## Validation report

Saved as `runs/<run_id>/ValidationReport.json` (spec §10). Each new validation run appends to `runs`.

```json
{
  "schema_version": 1,
  "run_id": "…",
  "runs": [
    {
      "artifact": "SemanticModel",
      "artifact_version": 3,
      "artifact_hash": "sha256:…",
      "attempt": 1,
      "started_at": "…",
      "passed": false,
      "checks": [
        {
          "id": "G4.DAX",
          "object": "measure:Net Revenue",
          "status": "fail",
          "severity": "error",
          "code": "DAX_REFERENCE",
          "message": "Column 'LineTotalX' not found",
          "evidence": { "query": "EVALUATE ROW(\"v\", [Net Revenue])", "response": "…" },
          "fingerprint": "sha256:…",
          "classification": "llm"
        }
      ]
    }
  ]
}
```

- `status` is `pass` | `fail` | `warning` | `skipped` (T-504: a check that needs the deployed model is `skipped` when deployment or refresh failed).
- `severity` is `error` | `warning` | `info`.
- `repair_attempt` (T-505) is set on a validation run made by the repair service, with the number of the attempt; the fingerprint is `sha256(check id, code, object and the message with numbers and ids stripped)`.
- `classification` is `deterministic` | `llm` | `user_input`. T-505 sets it.

## Object references

Model objects are referenced like this in plans, specs and reports:
- `{ "table": "Sales", "column": "OrderDate" }`
- `{ "measure": "Net Revenue" }`
- `{ "hierarchy": "Date", "table": "Date" }`
