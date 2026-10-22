# Fabric Hosting

**Decision (2026-10-21):** Nexora and its Rayfin UI are redeveloped to run **entirely on Microsoft Fabric**. The first build (T-001 to T-706) ran locally: a Next.js UI on `localhost:3000`, a FastAPI API on `localhost:8000`, a local `runs/` folder, `.env`, MSAL with OBO, and Application Insights. That code is the starting point, and its business logic (compiler, harvester, profiler, planner, generators, validators, repair, approvals, gates, impact, audit) is kept. What changes is **where it runs, how it is called, how it signs in, and where it stores things**.

The task plan in [`../tasks/`](../tasks/README.md) has been edited for this. Tasks whose code must change are `Not started` again. Tasks whose logic is unchanged and only need their acceptance criteria re-run on Fabric are `Needs Fabric re-test`.

## Target picture

```
 Fabric portal (user signed in to Fabric)
 ┌──────────────────────────────────────────────────────────────────────┐
 │ Rayfin = Fabric workload (Extensibility Toolkit), item "Nexora Run"  │
 │   React + Fluent UI v9, display rules reused from rayfin-ui/src/lib  │
 └───────────────┬───────────────────────────────┬──────────────────────┘
                 │ invoke function               │ start job / poll job
                 ▼                               ▼
 Nexora home workspace (one per environment)
 ┌──────────────────────────────────────────────────────────────────────┐
 │ UDF item  nexora_api         short, deterministic operations         │
 │ Notebook  nexora_stage_runner long / LLM / data / Fabric operations  │
 │ Environment nexora_env       the `nexora` wheel + langgraph, openai… │
 │ SQL Database nexora_control_db  dbo.nexora_runs, locks, access cache │
 │ Lakehouse nexora_control     Files/runs/<run_id>/…, Files/config/…   │
 │ Variable library nexora_settings   non-secret settings               │
 │ Eventhouse nexora_telemetry  logs, spans, metrics (T-704)            │
 └──────────────────────────────────────────────────────────────────────┘
        │ as the signed-in user                   │ Key Vault reference
        ▼                                         ▼
 Source workspaces / Lakehouses (user's scope)    Azure Key Vault (model key)
 Sandbox workspace (models, reports, bridges)     Azure OpenAI deployment
 Target workspace (promotion, T-702)
```

## Components

| Concern | First build (local) | On Fabric | Task |
|---|---|---|---|
| UI | `rayfin-ui/` Next.js app on port 3000 | Fabric workload frontend `rayfin-workload/` built with the Fabric Extensibility Toolkit; item type **Nexora Run**; pages as before; the pure display rules in `rayfin-ui/src/lib/*.ts` are moved over unchanged | T-104 and the UI parts of T-205, T-304, T-405, T-507, T-604, T-703 |
| API | FastAPI routes in `app/api/` | **User Data Functions** item `nexora_api` for short operations, plus **notebook jobs** of `nexora_stage_runner` for long ones. One dispatcher (`app/fabric/dispatch.py`) maps each logical operation (`POST /runs/{id}/compile` and so on) to the existing stage service. The map is in [CONTRACTS.md#fabric-operations](../tasks/CONTRACTS.md#fabric-operations) | T-102 |
| Long operations | synchronous HTTP call; deployment as a FastAPI background task | asynchronous job through the Fabric Job Scheduler API (`RunNotebook`, parameters `operation`, `run_id`, `payload`). The run records `active_job`; one job per run at a time (`409 RUN_BUSY`); the UI polls the run | T-102, T-702 |
| Identity | MSAL in the UI, JWT validation and OBO in the API | Fabric signs the user in. A UDF reads the caller from its invocation context (`oid`, `upn`, `name`). A notebook job runs as the user who started it, and gets user tokens through `notebookutils.credentials.getToken(...)`, so Fabric, OneLake, SQL endpoint and Power BI calls stay **user-delegated** without OBO. Local development keeps the T-002 interactive credential | T-002, T-105 |
| Run store | `FileRunStore` (`runs/<id>/RunManifest.json`) by default | `SqlRunStore` (Fabric SQL Database `nexora_control_db`, table `dbo.nexora_runs`) is the default for manifests. Every other run file goes under `Files/runs/<run_id>/` of the control Lakehouse `nexora_control`, through an `ArtifactStore` (OneLake in Fabric, the local folder in dev). `RUN_STORE=file` stays for local development | T-101 |
| Settings | `.env` | Variable library `nexora_settings` (non-secret settings), Key Vault for secrets, and `.env` only for local development. `app/config.py` reads shell variables first, then the variable library, then `.env` | T-001 |
| Config files | `config/*.yaml` in the repo | `Files/config/*.yaml` of the control Lakehouse, published from the repo by T-005 and still read on every call, so an edit applies without redeploying code | T-005 |
| Packaging | `.venv` and `pip install -r requirements.txt` | the `nexora` wheel built from `app/`, installed as a custom library in Environment `nexora_env`; `nexora_api` and `nexora_stage_runner` both use it. The items are deployed from the repo with Fabric Git integration or `fabric-cicd` | T-005 |
| Secrets | Key Vault through `DefaultAzureCredential`, else `.env` | Key Vault through `notebookutils.credentials.getSecret(KEY_VAULT_URL, name)` in jobs; UDFs never need the model key, because they make no model call | T-705 |
| Observability | OpenTelemetry to the console or Application Insights | OpenTelemetry exporter `eventhouse` (KQL database `nexora_telemetry` in the home workspace) by default, `azuremonitor` optional, `console` for dev. The Fabric Monitoring hub shows each job and function run | T-704 |
| Deployment lock | `runs/.locks/<workspace>.lock` file | a row in `dbo.nexora_locks` of the control database (`target_workspace_id` PK, `run_id`, `taken_at`) | T-702 |
| Render images | `GET /runs/{id}/files/{path}` | UDF `get_run_file` returns the file base64-encoded, with the same path rules. Users never get direct access to the control Lakehouse | T-604 |
| Run authorization | FastAPI dependency, workspace list through OBO | the dispatcher checks it before every operation. A creator passes in the UDF. For anyone else the decision needs the user's workspace list, which only a job can read, so it is cached in `dbo.nexora_run_access` for 5 minutes (see T-705) | T-705 |

## Operation hosting rule

- **UDF (`udf:`)** for operations that are deterministic, make no model call, touch only Nexora's own control items (SQL database, control Lakehouse) and finish well within the function timeout. Examples: create, list and get runs; approvals that only check gates; edits; gate checks; explain; data gaps; exceptions; return; impact; audit; trace; reads.
- **Job (`job:`)** for operations that call the model, read the user's chosen Lakehouse or SQL endpoint, call Fabric or Power BI REST, publish, deploy or validate, or could run long. Examples: compile, answer (which recompiles), scope binding, harvest, profile, relationships, sources, feasibility, bridges, measures, validation, repair, report plan and generation, regenerate, regenerate-stale, deploy.
- **UI (`ui:`)** only for read-only Fabric lookups that the Fabric host already allows the signed-in user, such as the workspace and Lakehouse pickers. The UI holds no business logic. Anything that decides, like binding a scope, is a job that checks it again.

Stage graphs (LangGraph) run **only inside jobs**, never inside a UDF. All the rules of [CONTRACTS.md#stage-graphs](../tasks/CONTRACTS.md#stage-graphs) still apply.

## Storage layout

```
nexora_control_db (Fabric SQL Database)
  dbo.nexora_runs        run_id PK, state, created_at, updated_at, version, manifest JSON   (T-101)
  dbo.nexora_locks       target_workspace_id PK, run_id, taken_at                           (T-702)
  dbo.nexora_run_access  tenant_id, workspace_id, user_oid, allowed, checked_at              (T-705)

nexora_control (Lakehouse)  Files/
  config/                roles.yaml, glossary.yaml, critical_terms.yaml, … (copied from repo config/)
  runs/<run_id>/         every run file the first build kept under runs/<run_id>/ except RunManifest.json:
                         BusinessContract.yaml, contracts/, MetadataInventory.json, inventories/,
                         approvals.jsonl, gates/, plans/, model/v<n>/, report/v<n>/, ValidationReport.json,
                         events.jsonl, returns.jsonl, Impact.json, deploy/backup-<ts>/, …
```

Relative paths inside a run folder are unchanged, so every artifact `path`, `backup_path` and hash keeps its meaning. Append-only files (`approvals.jsonl`, `events.jsonl`, `returns.jsonl`, `data_gaps.jsonl`) need append semantics on OneLake. The `ArtifactStore` rewrites the whole file and takes the run's job lock instead of appending in place. A UDF that appends takes the same lock.

## Local development mode

Every task must still run offline checks locally. `RUN_STORE=file`, `ARTIFACT_STORE=local`, `.env` and the interactive credential keep working, and the CLI (`python -m app.cli …`) keeps calling the store directly. The workload has a dev mode: the Extensibility Toolkit's local dev server, loaded into the Fabric portal in developer mode. In it, the frontend calls the real `nexora_api` in the dev home workspace. There is no local FastAPI in the target design. `app/api/` is kept only until T-102 replaces it, and then it is removed.

## Platform limits to verify when the task is worked on

These are not assumed facts. The task that first depends on each one checks it against the current Fabric documentation and records the result in this table.

| Item | Why it matters | Checked at | Result |
|---|---|---|---|
| UDF execution timeout and request/response size | Decides which operations may stay `udf:` | T-102 | **Checked against Microsoft's service-limits docs.** Request payload 4 MB, request execution timeout 240 s (100 s through a *public* function endpoint), response size 30 MB. Every `udf:` operation in CONTRACTS.md#fabric-operations is deterministic, touches only the control SQL database and control Lakehouse, and returns small JSON, so all of them fit comfortably; anything that calls the model, a user's Lakehouse/SQL endpoint, Fabric REST or Power BI stays `job:`. [Service details and limitations](https://learn.microsoft.com/en-us/fabric/data-engineering/user-data-functions/user-data-functions-service-limits) |
| UDF connections are bound at design time to specific items | A UDF can reach the control items only, never a user-chosen Lakehouse; hence the job rule | T-102 | **Confirmed.** `@udf.connection(argName=…, alias=…)` binds a named connection chosen in the portal's "Manage connections" UI ahead of time; there is no way to pass an arbitrary workspace/Lakehouse id at call time. `nexora_api` therefore only ever has connections to `nexora_control_db` and `nexora_control`. [Python programming model](https://learn.microsoft.com/en-us/fabric/data-engineering/user-data-functions/python-programming-model) |
| Whether a UDF can obtain a user-delegated Fabric token | If it can, run authorization needs no access cache (T-705) | T-105 | **Checked against Microsoft's docs: no.** A UDF's `@udf.generic_connection(argName, audienceType)` only mints a token for the **function item's owner identity** ("This feature generates a Microsoft Entra ID token with the item owner's identity"), never the calling user, and `audienceType` is limited to `CosmosDb` or `KeyVault` — there is no Fabric/Power BI/Storage/SQL audience at all. So a UDF can never call Fabric as the caller; only a job can (`notebookutils.credentials.getToken`, T-002). T-102's dispatcher therefore authorizes a `udf:` operation on ownership only (`created_by` match, or no owner) and refuses anyone else with `403 ACCESS_CHECK_REQUIRED` — the full workspace-list check (T-705) runs inside a job. [Python programming model](https://learn.microsoft.com/en-us/fabric/data-engineering/user-data-functions/python-programming-model) |
| What a UDF's invocation context actually exposes | Builds `app/fabric/identity.py:principal_from_udf_context` (T-105) | T-105 | **Confirmed.** `@udf.context(argName=…)` with a `fabric.functions.UserDataFunctionContext` parameter exposes `invocation_id` and `executing_user` (`Oid`, `TenantId`, `PreferredUsername`). **There is no UPN field** — only `PreferredUsername`, which Microsoft's own docs say is user-set and mutable, "shouldn't be used for authorization decisions". `principal_from_udf_context` therefore returns `{"oid": Oid, "upn": PreferredUsername, "name": PreferredUsername}` (`upn` is a display value only) and every ownership/authorization comparison in `can_access`/`authorize_run` compares `oid` first. [Python programming model](https://learn.microsoft.com/en-us/fabric/data-engineering/user-data-functions/python-programming-model) |
| ODBC Driver 18 and `pyodbc` in the notebook runtime | `FabricSQLConnector` (profiling, relationships, reconciliation) | T-002, T-003 | **Checked against public docs, not yet against a live notebook (T-002 AC-4 does that).** Microsoft's docs require ODBC Driver 18+ for the Warehouse/SQL analytics endpoint and recommend Entra ID auth; they don't say whether it ships in the default notebook pool image. `FabricSQLConnector._select_driver()` therefore tries `pyodbc` with Driver 18 first and falls back to Microsoft's pure-Python `mssql-python` package (no native ODBC driver needed) when that's missing, raising clearly if neither is available. [How to connect (Fabric Warehouse)](https://learn.microsoft.com/en-us/fabric/data-warehouse/how-to-connect) |
| Notebook job start latency | The UI must show "queued / running" for jobs | T-102, T-104 | Not yet checked against a live tenant; the UI shows "Queued" until `active_job.status` moves past `queued` regardless of how long that takes (T-104 AC-1). |
| Extensibility Toolkit hosting of frontend assets and tenant registration of the workload | A tenant admin may need to enable the workload; the frontend bundle may need a publisher host | T-104 | Not yet checked (T-104) |
| Job Scheduler API: a job runs as the user who started it, with notebook parameters | Keeps user-delegated access in jobs | T-102 | **Request shape confirmed; identity not yet confirmed against a live tenant.** `POST /v1/workspaces/{workspaceId}/items/{notebookId}/jobs/instances?jobType=RunNotebook` (equivalently `.../notebooks/{id}/jobs/execute/instances?jobType=RunNotebook`), body `{"executionData": {"parameters": {"job_id": {"value": "…", "type": "string"}, "run_id": {...}, "operation": {...}, "payload": {"value": "<JSON text>", "type": "string"}}}}` (a forum-confirmed shape; the top-level `parameters` key other samples show does not work). Poll `GET .../jobs/instances/{jobInstanceId}` for `status` (`NotStarted`/`InProgress`/`Completed`/`Failed`/`Cancelled`) and `failureReason`. That a `RunNotebook` job started this way runs as the caller (not a fixed service identity) is the existing T-002 assumption, carried over from `notebookutils.credentials.getToken` docs; still to be confirmed by running `nexora_stage_runner` from two different users' tokens (T-102 AC-7/AC-8, T-105 AC-5). [Notebook public API](https://learn.microsoft.com/en-us/fabric/data-engineering/notebook-public-api) |
| Custom wheel in an Environment used by both UDF and notebook | Packaging | T-005 | See T-005's notes below; not yet run against a live tenant |
| Whether a job's `pbi`-audience `notebookutils` token is sufficient for Power BI dataset refresh and `executeQueries` (T-504), and for the Fabric REST item/notebook calls T-506's bridge generation makes, without any further tenant-level grant | The first build needed its own Entra app registration's delegated `Dataset.ReadWrite.All` (with consent) for these calls via OBO; under Fabric there is no app registration, only the job's own `notebookutils` token | T-504 | Not yet checked against a live tenant. `app/fabric/deps.py`'s `items_client()`/`powerbi_client()` already default to `get_token(FABRIC_SCOPE)`/`get_token(POWERBI_SCOPE)` (T-002), so no code change is expected either way; T-504's own manual test (validate a model as a job and watch for `403`/insufficient-scope errors) is where this gets confirmed. |
| Whether an Eventstream custom endpoint accepts a plain batched-JSON `POST` of arbitrary rows, and which token audience it needs | T-704's `app/connectors/eventhouse.py:EventhouseSink` posts telemetry rows there by default | T-704 | **Not yet confirmed against a live tenant.** `notebookutils.credentials.getToken`'s documented `kusto` audience (`app.auth.KUSTO_SCOPE = "https://kusto.kusto.windows.net/.default"`, a generic resource, not a cluster-specific one) is what `EventhouseSink` requests by default; whether that's accepted by an Eventstream custom endpoint, or whether the endpoint instead needs no auth (a SAS-style URL) or a direct Kusto ingestion endpoint's own audience, is unconfirmed. If this turns out wrong, only `eventhouse.py`'s `_token()` and `NEXORA_EVENTHOUSE_URI`'s value need to change — `app.observability.telemetry`'s exporter classes just call `EventhouseSink.send(table, rows)` and don't know how it authenticates. T-704's own manual test (watch `nexora_telemetry` for rows after a run) is where this gets confirmed. |
| Which identity `notebookutils.credentials.getSecret(KEY_VAULT_URL, name)` resolves as inside a job: the job's own user, or the workspace's identity | Decides who needs the Key Vault Secrets User role for `azure-openai-api-key` (T-705) | T-705 | Not yet checked against a live tenant; `app/security/secrets.py:read_vault_secret` just calls the documented API and doesn't need to know which identity it resolves as, but the Key Vault access policy / RBAC assignment does. Record the answer here once T-705's own manual test (AC-1) confirms it. |

### `notebookutils.credentials.getToken` audiences (T-002)

Checked against Microsoft's `notebookutils credentials` docs, not yet against a live job (T-002 AC-2/AC-3 do that). The documented short audience keys are `storage`, `pbi`, `keyvault` and `kusto`; `pbi` covers both the Power BI REST API and the Fabric REST API (`api.fabric.microsoft.com`). There is no short key for the SQL endpoint audience — community samples call `getToken` with the raw `https://database.windows.net/.default` resource string directly, which `app/auth.py` also uses. Mapping used by `app/auth.py:_JOB_AUDIENCE_BY_SCOPE`:

| Scope constant | notebookutils audience |
|---|---|
| `STORAGE_SCOPE` | `"storage"` |
| `POWERBI_SCOPE` | `"pbi"` |
| `FABRIC_SCOPE` | `"pbi"` (same audience as Power BI) |
| `SQL_SCOPE` | the raw scope string (no short key documented) |

`notebookutils.runtime.context` documents `userId` and `userName` properties but not a separate UPN; `app/auth.py:current_identity()` uses `userName` for both `upn` and `name` until this is confirmed against a live tenant. [notebookutils credentials](https://learn.microsoft.com/en-us/fabric/data-engineering/notebookutils/notebookutils-credentials) · [notebookutils runtime](https://learn.microsoft.com/en-us/fabric/data-engineering/notebookutils/notebookutils-runtime)

### Variable library from a notebook (T-001)

Checked against the Fabric Updates Blog and Microsoft's `notebookutils variable library` docs: notebook support for reading a Variable library is generally available. `app/config.py` calls `notebookutils.variableLibrary.getLibrary("nexora_settings")` once per process and reads each setting with `library.getVariable(name)`. Not yet run against a live tenant (T-001 AC-6). [Variable library support in notebook (GA)](https://blog.fabric.microsoft.com/en/blog/variable-library-support-in-notebook-now-generally-available) · [notebookutils variable library](https://learn.microsoft.com/en-us/fabric/data-engineering/notebookutils/notebookutils-variable-library)
