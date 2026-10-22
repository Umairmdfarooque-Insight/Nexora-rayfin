# M1 — Foundation

**Exit deliverable:** a user can start and inspect a run from Rayfin in the Fabric portal. Shared shapes are in [CONTRACTS.md](CONTRACTS.md); the hosting design is in [fabric-hosting.md](../technical-documentation/fabric-hosting.md).

> **Fabric rework.** Every M1 task was rewritten for Fabric hosting and is `Not started` again. The first build (FastAPI, Next.js, MSAL/OBO, local `runs/` folder) is the starting code: keep its state machine, store interface, domain errors, `next_action` and the UI's display rules, and replace the hosting around them.

---

### T-101 — Run model, state machine and run store

**Depends on:** T-005 · **Spec:** FR-001, FR-021, §11 Workflow State Model, `RunManifest.json` (§10), NFR-003 · **Fabric hosting:** library code used by every UDF and job; the CLI for local and admin use

#### Problem statement
The first build has a working run model, state machine, `FileRunStore` and `SqlRunStore`. Every other run file (contracts, inventories, approvals, gates, model and report folders) is written straight to the local `runs/<run_id>/` folder by many modules. In Fabric there is no local folder that a UDF and a job share. Manifests must live in the control SQL database, and every other run file must go to OneLake (`Files/runs/<run_id>/` of `nexora_control`) through one interface, so the stage code doesn't depend on where it runs.

#### Functional requirements
1. Keep the first build's behaviour of `RunState`, `ALLOWED`, `can_transition`, `Run`, `apply_transition`, `RunStore` and its errors, unchanged. That covers creation, unique `run_id`, one transition function, history, `resume_state`, and the CLI `create`/`list`/`show`/`transition`.
2. `SqlRunStore` becomes the **default in Fabric** (`RUN_STORE=fabric_sql` in the Variable library). `FileRunStore` stays the default locally. The table also gets the column `active_job` (JSON, nullable), used by T-102.
3. New **artifact store**: `app/store/artifacts.py`, `ArtifactStore(Protocol)`. Every module that read or wrote a file under `runs/<run_id>/` (other than `RunManifest.json`) goes through it:
   - `LocalArtifactStore(root)` uses the local folder, as before.
   - `OneLakeArtifactStore(workspace_id, lakehouse_id)` uses `Files/runs/` through the OneLake DFS API. In a job it may use the default-Lakehouse mount `/lakehouse/default/Files/runs/`. In a UDF it uses the function's Lakehouse connection.
   - `ARTIFACT_STORE=local|onelake` picks it, through `get_artifact_store()`.
4. Relative paths inside a run folder are unchanged, so artifact `path`, `backup_path`, hashes and `folder_digest` keep their values.
5. `app/fsutil.py:replace_file` and the `*.tmp` + rename pattern become `ArtifactStore.write_atomic(run_id, rel_path, data)`. Append-only files (`approvals.jsonl`, `events.jsonl`, `returns.jsonl`, `data_gaps.jsonl`) use `ArtifactStore.append_line(run_id, rel_path, line)`, which keeps them append-only under concurrent writers: it rewrites the file under the run's lease (FR-6) and never loses a line.
6. **Run lease:** `RunStore.lease(run_id, owner, seconds=900)` / `release(run_id, owner)`. It is a row-level lock in `dbo.nexora_runs` (a lock file locally). Writers of a run's files take it, so a UDF edit and a running job can't interleave.
7. CLI commands that take a run folder (`contract validate`, `plan validate`, `report validate`, `model generate`) accept `runs/<id>`, and resolve it through the artifact store when `ARTIFACT_STORE=onelake`. A command that needs a real folder downloads the run's files to a temporary folder first. New `python -m app.cli runs pull RUN_ID [--to DIR]` copies a run's files from OneLake to a local folder for inspection and diffs; it is read-only towards Fabric.

#### API contracts
- `app/workflow/states.py`, `app/workflow/run.py`, `app/store/base.py`, `app/store/file_store.py` and `app/store/sql_store.py`: as in the first build, plus `lease`/`release` and the `active_job` column.
- `app/store/artifacts.py`:
  - `read_bytes(run_id, rel) -> bytes` (raises `NotFound`)
  - `write_atomic(run_id, rel, data: bytes) -> None`
  - `append_line(run_id, rel, line: str) -> None`
  - `exists(run_id, rel) -> bool`
  - `list(run_id, prefix) -> list[str]`
  - `copy_tree(run_id, src, dst) -> None`
  - `local_path(run_id, rel) -> Path | None`: a real path when one exists (local, or the job mount), for tools that need a folder, such as the TMDL and PBIR writers. Otherwise `None`, and the caller stages in a temporary folder and uploads with `copy_tree`.
- `app/store/factory.py`: `get_run_store()` and `get_artifact_store()`, reading `RUN_STORE`, `ARTIFACT_STORE`, `FABRIC_CONTROL_DB_SERVER`, `FABRIC_CONTROL_DB_NAME`, `NEXORA_HOME_WORKSPACE_ID` and `NEXORA_CONTROL_LAKEHOUSE_ID` from `app.config`.
- CLI `python -m app.cli runs …`: unchanged (`create`, `list`, `show`, `transition`; exit `0`/`1`/`2`). Locally it can point at the Fabric stores by setting `RUN_STORE=fabric_sql` and `ARTIFACT_STORE=onelake`.

#### Constraints
- The store is the only code that writes manifests, and the artifact store is the only code that writes run files.
- The SQL store targets a writable Fabric **SQL Database** (`nexora_control_db`), never a read-only Lakehouse SQL endpoint.
- No LangGraph here. `RunStore` stays the only source of truth for run state.
- `runs/` stays gitignored locally.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| Empty or whitespace-only prompt | `ValueError("Prompt cannot be empty")`; the CLI exits `2`. |
| Prompt longer than 10,000 characters | Rejected with the limit. |
| Unknown `run_id` | `RunNotFound`; the CLI prints `Run not found: <id>` and exits `1`. |
| Illegal transition, or a transition to the current state | `IllegalTransition`; the state and history are unchanged. |
| Corrupt manifest | `get` raises `RunCorrupt`; `list` skips the run and warns. |
| `RUN_STORE` or `ARTIFACT_STORE` invalid | `ValueError` naming the valid values; CLI exit `2`. |
| Control database or Lakehouse setting blank | `ValueError: Missing required configuration: <name>`; CLI exit `2`. |
| Control database or OneLake unreachable or forbidden | `StoreUnavailable` with the reason; CLI exit `1`. |
| Another writer changes a run during a transition | Re-read and re-validate up to 3 times, then `StoreUnavailable`. |
| Lease held by another owner | Wait up to 30 s, then `409 RUN_BUSY`. A lease past its expiry is taken over and logged. |
| Two concurrent `append_line` calls on one file | Both lines are present, in order of lease acquisition. |

#### Acceptance criteria
1. **Local create.** With `RUN_STORE=file`, run `python -m app.cli runs create --prompt "Total sales by month"`. **Expected:** a UUID, and `runs/<id>/RunManifest.json` with `"state": "CREATED"` and one history entry.
2. **Rules unchanged.** Repeat the first build's checks: a second create gives a different id; `runs transition <id> DEPLOYING` is refused (exit `1`, state still `CREATED`); `runs transition <id> NEEDS_CLARIFICATION --reason test` adds a history entry; `runs transition <id> NEEDS_INPUT` sets `resume_state: NEEDS_CLARIFICATION`.
3. **Fabric stores.** In a home-workspace notebook (or locally with `RUN_STORE=fabric_sql` and `ARTIFACT_STORE=onelake`), create a run and write `get_artifact_store().write_atomic(run_id, "probe/hello.txt", b"hi")`. **Expected:** `SELECT run_id, state FROM dbo.nexora_runs` returns the run, and `nexora_control` → `Files/runs/<id>/probe/hello.txt` exists.
4. **Append is safe.** In the notebook, start 5 threads that each call `append_line(run_id, "probe/log.jsonl", …)` 20 times. **Expected:** 100 lines, none lost or interleaved.
5. **Same rules in SQL.** Repeat AC-2 against the SQL store. **Expected:** the same results, and the `state` column matches `runs show`.
6. **Missing setting.** With `RUN_STORE=fabric_sql`, blank `FABRIC_CONTROL_DB_NAME` and run `runs list`. **Expected:** `Missing required configuration: FABRIC_CONTROL_DB_NAME`, exit `2`, no traceback.
7. **Unreachable database.** Set `FABRIC_CONTROL_DB_SERVER=invalid.example.com` and run `runs list`. **Expected:** a readable "Cannot connect" message, exit `1`.
8. **Corrupt manifest tolerated.** With the file store, truncate one manifest to `{` and run `runs list`. **Expected:** the other runs are listed, with a warning naming the corrupt one.

---

### T-102 — Fabric orchestration surface (User Data Functions and stage jobs)

**Depends on:** T-101 · **Spec:** FR-001, FR-021, §7 (API and orchestration layer), NFR-003, NFR-005 · **Fabric hosting:** builds `nexora_api` and `nexora_stage_runner`

#### Problem statement
The first build exposes every operation as a FastAPI route on `localhost:8000`, and runs long operations synchronously inside the HTTP call. In Fabric there is no server process. Short operations must be User Data Functions, and long ones (model calls, source queries, Fabric and Power BI calls) must be notebook jobs that the UI starts and polls. The operation names, bodies, status codes and error envelope that every later task specifies must not change. Only the transport changes.

#### Functional requirements
1. **Dispatcher.** `app/fabric/dispatch.py` holds the table of operations in [CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations). It maps a method and path pattern to the existing service function, its request model, its success status and its host (`udf` or `job`). It converts exceptions to the error envelope with the mapping of `app/api/errors.py`, which moves to `app/fabric/errors.py`. Path and body validation keep the pydantic models of `app/api/schemas.py`, which move to `app/fabric/schemas.py`.
2. **UDF item `nexora_api`.** `call(method, path, query, body)` and `me()` per CONTRACTS. The caller comes from the invocation context (T-105). Calling a `job` operation through `call` gives `400 BAD_REQUEST` "start it as a job".
3. **Jobs.** `POST /runs/{id}/jobs` reserves `active_job`, after the operation's cheap preconditions pass: the state, the required artifacts, no active job (`409 RUN_BUSY`). The UI then starts `nexora_stage_runner` with the Job Scheduler API. The notebook:
   1. records `job_instance_id` and sets `running`;
   2. takes the run lease;
   3. calls the dispatcher as the job's user;
   4. writes `runs/<id>/jobs/<job_id>.json` (`{"status", "body"}`);
   5. sets `succeeded` or `failed` and releases the lease.

   `GET /runs/{id}/jobs/{job_id}` returns that file (`404` until it exists).
4. The first build's read operations move to `udf`: `GET /health`, `POST /runs`, `GET /runs`, `GET /runs/{id}` and `GET /runs/{id}/artifacts`. They keep the same shapes, with `RunDetail` gaining `active_job`.
5. **`next_action` is unchanged**, with one addition: while `active_job.status` is `queued` or `running`, `next_action` is `Wait for <operation label>`.
6. **Retire FastAPI.** Remove `app/api/` (routes, `main.py`, `deps.py`, `security.py`, `obo.py`, CORS) once every operation is in the dispatcher. The CLI and the manual scripts keep working.
7. A reservation without a `job_instance_id` after 10 minutes is released as `cancelled`. A job whose notebook run ended without a result file is marked `failed` with `502 JOB_FAILED`. Both checks run whenever the run is read.

#### API contracts
- [CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations) is the contract.
- `fabric/nexora_api/function_app.py` holds the UDF definitions. `call` and `me` are thin wrappers over `app.fabric.dispatch`. Its connections are `nexora_control_db` and `nexora_control`.
- `fabric/nexora_stage_runner.Notebook/`: a parameter cell (`job_id`, `run_id`, `operation`, `payload`), then `from app.fabric.job import run_job; run_job(...)`. The exit value is the result JSON.
- `app/fabric/job.py`: `run_job(job_id, run_id, operation, payload) -> dict`.
- `GET /health` returns `200 {"status": "ok", "app": "Nexora", "version": "<wheel version>", "runtime": "fabric_udf"}`.
- The `next_action` table is the one from the first build (CREATED → "Compile requirement" … ROLLED_BACK → "Investigate and redeploy").

#### Constraints
- UDF and job handlers hold no business logic. They call the dispatcher, which calls the stage services.
- There is no operation for arbitrary state transitions.
- Stage graphs run only in jobs ([CONTRACTS.md#stage-graphs](CONTRACTS.md#stage-graphs) rule 8).
- Check the UDF timeout and payload limits and the job-start latency against current Fabric documentation, and record them in `fabric-hosting.md`. If an operation marked `udf` can exceed the timeout, move it to `job` in CONTRACTS.md on the same branch.
- If a UDF can start a notebook job as the calling user, merge the reserve and start steps as CONTRACTS describes. The UI contract stays the same.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| Empty prompt or missing `prompt` | `422 VALIDATION_ERROR` naming the field. |
| Unknown `run_id` | `404 RUN_NOT_FOUND`. |
| `limit` above 200 or below 1; invalid `state` filter | `422`; for the state filter the error lists the valid states. |
| Unknown method or path | `404 NOT_FOUND`. |
| `job` operation called through `call` | `400 BAD_REQUEST` "start it as a job". |
| A job is already queued or running for the run | `409 RUN_BUSY` with `details.active_job`. |
| Preconditions fail at reservation | The operation's own error (`409 WRONG_STATE`, `404 NOT_FOUND`, …), and nothing is reserved. |
| Notebook crashes or is cancelled | `active_job.status: failed`, `error` `502 JOB_FAILED` with the `job_instance_id`. The run state is whatever the service had committed. |
| Unhandled exception | `500 INTERNAL_ERROR`, generic message; details only in the log. |

#### Acceptance criteria
1. **Health.** In the Fabric portal, open `nexora_api` and test `call("GET", "/health")`. **Expected:** `status 200` with `runtime: fabric_udf`.
2. **Create.** Test `call("POST", "/runs", body={"prompt": "Sales by product category"})`. **Expected:** `status 201`, a `run_id`, `state: CREATED`, `next_action: "Compile requirement"`, `active_job: null`.
3. **Read and list.** Test `GET /runs/{id}` and `GET /runs`. **Expected:** the new run appears in both.
4. **Validation and not found.** Test `POST /runs` with `{"prompt": ""}`, then `GET /runs/00000000-0000-0000-0000-000000000000`. **Expected:** `422` with the envelope, then `404 RUN_NOT_FOUND`.
5. **Job round trip.** Test `POST /runs/{id}/jobs` with `{"operation": "POST /runs/{id}/compile", "body": {}}`. **Expected:** `202` with a `job_id`. Then run `nexora_stage_runner` from the portal with those parameters. **Expected:** `active_job` goes `queued → running → succeeded`, `GET /runs/{id}/jobs/{job_id}` returns the compile response, and the state moves as T-202 specifies.
6. **One job at a time.** While a job is running, reserve another. **Expected:** `409 RUN_BUSY`.
7. **Crash handled.** Cancel a running job from the Monitoring hub, then read the run. **Expected:** `active_job.status: failed` with `JOB_FAILED`, and `next_action` is no longer "Wait…".
8. **Shared store.** Locally with the Fabric stores, run `python -m app.cli runs list`. **Expected:** it shows the runs created through `nexora_api`.

---

### T-103 — Workspace and Lakehouse selection (UI pickers, scope bound by a job)

**Depends on:** T-102 · **Spec:** FR-004, §14 (authorize by workspace and Lakehouse) · **Fabric hosting:** pickers `ui:`; `PUT /runs/{id}/scope` is a job

#### Problem statement
Each run needs a workspace, a Lakehouse and optionally schemas that the user can access. Connectors must use that choice. In the first build the API listed and verified these through OBO. In Fabric a UDF can't call Fabric as the user, so the pickers read Fabric directly with the user's token, and binding the scope (the decision) runs as a job that verifies it again as the user.

#### Functional requirements
1. The Rayfin pickers list the workspaces and the Lakehouses (with SQL endpoint details) visible to the signed-in user, by calling Fabric REST with the workload host's token for the user. This is display only.
2. `PUT /runs/{id}/scope` runs as a job. As the user, it verifies that the workspace, the Lakehouse and each schema exist and are visible (`bind_scope` from the first build), copies `workspace_name`, `lakehouse_name` and `sql_endpoint`, and saves the scope.
3. Connectors read the workspace, Lakehouse and SQL endpoint from the **run scope**, through `FabricLakehouseConnector.for_scope(scope)` and `FabricSQLConnector.for_scope(scope)`, as before. The `FABRIC_*` settings are only a fallback for scripts that don't use a run.
4. The scope job also records `{tenant_id, workspace_id, user_oid, allowed: true}` in the T-705 access cache. Until T-705 it is just written.

#### API contracts
| Operation | Host | Request | Response |
|---|---|---|---|
| Workspace picker | ui: `GET https://api.fabric.microsoft.com/v1/workspaces` | — | the UI's list |
| Lakehouse picker | ui: `GET https://api.fabric.microsoft.com/v1/workspaces/{id}/lakehouses` | — | the UI's list, `sql_endpoint` from `properties.sqlEndpointProperties` |
| `PUT /runs/{id}/scope` | job | `{"workspace_id", "lakehouse_id", "schemas"?: [str]}` | `200` `RunDetail` |

- `GET /fabric/workspaces` and `GET /fabric/workspaces/{id}/lakehouses` are no longer Nexora operations.
- The `scope` shape and `RunDetail.warnings` are unchanged ([CONTRACTS.md#run](CONTRACTS.md#run)).
- `workspace_id` and `lakehouse_id` must be GUIDs (`422`). A workspace that doesn't exist is `403 FORBIDDEN`, the same as one the user can't see.

#### Constraints
- The job uses the T-002 provider, so it acts as the user. No OBO, no service identity.
- Follow `continuationToken` paging in the pickers and in the job.
- The scope can change only before metadata is harvested: `CREATED`, `NEEDS_CLARIFICATION`, or `REQUIREMENT_APPROVED` with no `MetadataInventory` artifact.
- The pickers' result is never trusted. The job decides.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| Workspace or Lakehouse not visible to the user | `403 FORBIDDEN`; the scope isn't saved. |
| Lakehouse with no SQL endpoint yet | `sql_endpoint: null`; allowed, with a warning. |
| Fabric returns `429` | Retry after `Retry-After`, up to 3 times, then `429 RATE_LIMITED`. |
| Fabric unavailable or `5xx` | `502 UPSTREAM_ERROR` with the Fabric request ID. |
| Scope change after harvest | `409 SCOPE_LOCKED`, checked when the job is reserved. |
| Unknown schema | `404 SCHEMA_NOT_FOUND` naming it. |
| Picker call fails in the UI | The UI shows the error and a retry; no run changes. |

#### Acceptance criteria
1. **Pickers match the portal.** In Rayfin's New run page (after T-104), or in the browser console of the workload in dev mode, list workspaces and the Lakehouses of a known workspace. **Expected:** the same as the Fabric portal, with `sql_endpoint` filled in.
2. **Bind scope.** Start the scope job for a run with valid ids and `"schemas": ["saleslt"]`, then read the run. **Expected:** `scope` is shown with names and the SQL endpoint.
3. **Invalid id refused.** Start the scope job with a random GUID as `workspace_id`. **Expected:** the job result is `403`, and the run still shows the previous scope.
4. **Bad schema.** Bind with `"schemas": ["does_not_exist"]`. **Expected:** `404 SCHEMA_NOT_FOUND`.
5. **Locked after harvest.** After T-301, try to reserve a scope job for a harvested run. **Expected:** `409 SCOPE_LOCKED` at reservation.

---

### T-104 — Rayfin workload shell in Fabric

**Depends on:** T-103 · **Spec:** §8.1 Rayfin Web UI, FR-001, FR-004, FR-021, NFR-005, §7 (React, Fluent UI) · **Fabric hosting:** builds the Fabric workload `rayfin-workload/`

#### Problem statement
Business users need Rayfin inside Fabric, next to their workspaces, not a separate site on `localhost:3000`. The first build's Next.js pages and display rules exist. They have to become a Fabric workload whose item type, **Nexora Run**, opens the run pages, and they have to talk to `nexora_api` and the stage runner instead of FastAPI.

#### Functional requirements
1. A Fabric workload built with the **Fabric Extensibility Toolkit** in `rayfin-workload/` (React, TypeScript, Fluent UI v9). It provides:
   - a **Rayfin** home with the run list;
   - a **New run** experience with the prompt editor, subject area, desired outputs, constraints, and the workspace and Lakehouse pickers (T-103);
   - the item type **Nexora Run**, whose editor is the run detail page: prompt, scope, state, next action, open questions, timeline and `active_job` progress.
2. `src/lib/api.ts` is rewritten as a client of `nexora_api`. `call(method, path, …)` invokes the UDF through the workload host. `runJob(operation, body)` reserves, starts `nexora_stage_runner` through the Job Scheduler API, polls `GET /runs/{id}` every 2 s (then every 5 s after 30 s), and returns the job result. The pages keep calling it by operation name.
3. The pure display rules of the first build (`rayfin-ui/src/lib/*.ts`: `review.ts`, `inventory.ts`, `plan.ts`, `model.ts`, `report.ts`, `audit.ts`) and their unit tests are moved over unchanged. Pages are ported as later tasks are redone: this task ports `/runs`, `/runs/new` and `/runs/[id]`.
4. Long operations show their job state, "Queued" or "Running · <elapsed>", and disable other actions on the run until the job finishes.
5. Creating a Nexora Run item in a workspace starts a run (`POST /runs`). The item stores only the `run_id`. The run itself lives in the control store.

#### API contracts
- Folder `rayfin-workload/`, the Extensibility Toolkit project layout: the manifest with item type `Nexora.Run`, the frontend, and `npm test` for the display rules.
- Commands: the Toolkit's dev server (`npm start`, loaded into the Fabric portal in developer mode), `npm test`, `npm run build`.
- Settings in the workload configuration, not in source: the home workspace id, the `nexora_api` item id and the `nexora_stage_runner` item id.
- The workload registration and publishing steps (tenant admin enablement, the Entra app the Toolkit needs, the frontend host if the Toolkit still needs one) are recorded in `fabric-hosting.md` and in `DEVELOPMENT_WORKFLOW.md`.

#### Constraints
- No business logic in the UI. It shows what `nexora_api` returns, including `next_action`.
- The run detail page shows the state, the next action, the blocker and the job progress above the fold at 1366×768, inside the Fabric item editor.
- Accessible: keyboard navigable, with labels on every input.
- `rayfin-ui/` stays as reference until the last UI task (T-703) is ported, then it is removed.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| `nexora_api` unreachable or the function fails | An error banner with a retry button. The typed prompt is kept. |
| Workspace has no Lakehouses | "No Lakehouses in this workspace"; submit disabled. |
| No runs yet | Empty state with "Create your first run". |
| `422` from an operation | The field-level message next to the input. |
| A job takes longer than 1 s to start | "Queued" indicator; the run's other actions are disabled. |
| A job fails | The job's error envelope in a banner, with the job instance id. |
| `409 RUN_BUSY` | "Another operation is running on this run", and the page refreshes. |

#### Acceptance criteria
1. **Create in Fabric.** In the Fabric portal (workload in developer mode), open Rayfin → **New run**, enter a prompt, choose a workspace and Lakehouse, and submit. **Expected:** the run page opens with state `CREATED`, the chosen scope (bound by the scope job, with its progress shown) and "Compile requirement" as the next action.
2. **Listed.** Open the Rayfin home. **Expected:** the new run is at the top.
3. **One-screen status.** On the run page at 1366×768. **Expected:** the state, next action, job progress and timeline are visible without scrolling.
4. **Function down.** Remove the `nexora_api` id from the workload configuration (or stop the function) and submit a new run. **Expected:** an error banner, and the prompt text is still there.
5. **Validation shown.** Submit an empty prompt. **Expected:** an inline message on the prompt field.
6. **Display rules intact.** Run `npm test` in `rayfin-workload/`. **Expected:** the first build's display-rule tests pass unchanged.

---

### T-105 — Fabric identity and attribution

**Depends on:** T-104 · **Spec:** NFR-001, §14, FR-004, FR-018 (attributable actions) · **Fabric hosting:** identity in the workload, in `nexora_api` and in jobs

#### Problem statement
The first build signed users in with MSAL, validated bearer tokens in FastAPI and called Fabric through OBO. In Fabric the user is already signed in to the portal, and the workload, the functions and the jobs each learn who the user is from Fabric. FR-004 still requires that users see only what they can access, and FR-018 that every action is attributable. This has to hold without a custom token validator and without OBO.

#### Functional requirements
1. Rayfin uses the Fabric host's sign-in: no MSAL login page of its own. The workload requests tokens for Fabric REST from the host when the pickers and the Job Scheduler call need them.
2. `nexora_api` reads the caller from the UDF invocation context and builds the principal `{oid, upn, name}`. A call without a user context (anonymous, or a service principal not allowed by config) is `401 UNAUTHENTICATED`, except `GET /health`.
3. A job reads its user through `app.auth.current_identity()` (T-002). The dispatcher compares it with the user who reserved the job. A mismatch is `403 FORBIDDEN`, and the job writes nothing.
4. Runs record `created_by`, and history entries record `actor` as the user's UPN, as before. Approval records, audit events and deployments take the same principal.
5. Fabric, OneLake, SQL endpoint and Power BI calls run as the user, because only jobs make them (T-002). Lists therefore reflect the user's access.
6. Remove the first build's `api/security.py`, `api/obo.py`, `ENTRA_API_CLIENT_ID`, `ENTRA_API_CLIENT_SECRET` and the UI's `NEXT_PUBLIC_ENTRA_*` settings, unless the Extensibility Toolkit still needs an Entra app. In that case, record its settings in `fabric-hosting.md`.

#### API contracts
- `app/fabric/identity.py`: `principal_from_udf_context(ctx) -> {oid, upn, name}` and `principal_from_job() -> {oid, upn, name}`.
- The dispatcher passes the principal to every service, as `current_user` did.
- `RunStore.create(…, created_by)`: unchanged. CLI runs keep `created_by: null` and actor `system`.
- The platform-limits row "Whether a UDF can obtain a user-delegated Fabric token" in `fabric-hosting.md` is filled in here.

#### Constraints
- No bypass. A missing user context is never treated as "system".
- Never log tokens or the raw invocation context.
- Trust only the identity Fabric provides. Ignore any user fields in the request body.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| Function invoked without a user (for example a service principal) | `401 UNAUTHENTICATED`. |
| Job run by a different user than the one who reserved it | `403 FORBIDDEN`; the reservation stays `queued` until it expires. |
| User lacks consent for a scope a job needs | `403 CONSENT_REQUIRED` naming the scope. |
| User has no Fabric workspaces | The picker shows "You don't have access to any Fabric workspace". |
| User not allowed to run notebooks in the home workspace | Starting the job fails in the UI with Fabric's message. The run is unchanged, and the reservation expires. |

#### Acceptance criteria
1. **Signed in by Fabric.** Open Rayfin in the Fabric portal in a private window. **Expected:** the Fabric sign-in only, with no separate Rayfin login.
2. **Attribution.** Create a run. **Expected:** the run page shows **Created by: \<your UPN\>**, and the first history entry's `actor` is your UPN.
3. **Anonymous refused.** Invoke `nexora_api` `call("GET", "/runs")` without a user context, for example from a service principal. **Expected:** `401 UNAUTHENTICATED`. `GET /health` still returns `200`.
4. **Per-user resources.** Sign in as a second test user with access to fewer workspaces. **Expected:** the pickers show only that user's workspaces, and binding one of the first user's workspaces fails with `403`.
5. **Job identity.** Reserve a job as user A and start the notebook as user B. **Expected:** `403 FORBIDDEN`, and the run is unchanged.
6. **M1 exit.** As a signed-in user, create a run with a scope and open its page in the Fabric portal. **Expected:** this works end to end without the CLI or a test pane.
