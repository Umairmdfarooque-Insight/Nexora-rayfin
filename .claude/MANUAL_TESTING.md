# Manual testing guide

How to test each Nexora task by hand. A task is `Done` only after its checks here pass (`/implement-task done T-xxx`).

**This file is updated every time a task is implemented.** `/implement-task` adds or refreshes the task's section here at hand-over (see [Adding a task](#adding-a-task)). Acceptance criteria are defined in `.claude/tasks/`; this file holds the exact commands.

> **Fabric redevelopment (2026-10-21).** The sections below were written for the first, locally hosted build (FastAPI on `localhost:8000`, Next.js on `localhost:3000`, `.env`, local `runs/`). They still apply as-is only to tasks whose status in [`tasks/README.md`](tasks/README.md) is `Done` (T-004, T-201).
> - For every other task the steps are **superseded**. Use the task's acceptance criteria and its Fabric note in `.claude/tasks/`.
> - `/implement-task` rewrites a task's section here for Fabric when it hands the task over.
> - On Fabric, `udf` operations are tested from Rayfin or the `nexora_api` test pane, and `job` operations from Rayfin or by running `nexora_stage_runner` (see [CONTRACTS.md#fabric-operations](tasks/CONTRACTS.md#fabric-operations)).

## Before you start

- Windows, PowerShell, Python 3.12. Run everything from the **project root**: `cd "C:\Users\umdfaroo\Downloads\NEXORA-main\NEXORA-(Rayfin)"`. Python only finds the `app` package from there.
- Activate the virtualenv: `.venv\Scripts\activate`. If it fails with `No Python at ...`, recreate it: `py -3.12 -m venv .venv`, then `pip install -r requirements.txt`.
- `.env` must exist (copy `.env.example`) with real values. Never paste its values into chat or commit it.
- If a test tells you to edit `.env` (comment out a line, blank a value), **restore it afterwards**.
- Tests marked 🌐 need a live Azure/Fabric sign-in (browser). Tests marked 💻 run offline.

---

## M0 — Baseline

> T-001, T-002 and T-003 were rewritten for the Fabric redevelopment (2026-10-21). The steps below replace the first-build ones. T-005 is new.

### T-001 — Dependencies and configuration loading (Variable library, `.env` for dev)

1. 💻 **Clean install.** `Remove-Item -Recurse -Force .venv; py -3.12 -m venv .venv; .venv\Scripts\activate; pip install -r requirements.txt; pip wheel . --no-deps -w dist`. **Expected:** both succeed, and `dist\` holds `nexora-*.whl`.
2. 💻 **Defaults load locally.** `python -c "import app.config as c; from app.runtime import RUNTIME; print(c.APP_NAME, RUNTIME)"`. **Expected:** `Nexora local`.
3. 💻 **Missing setting.** In `.env`, put `#` at the start of the `AZURE_OPENAI_API_KEY` line (or blank its value), then `python -c "import app.llm.client"`. **Expected:** `ValueError: Missing required configuration: AZURE_OPENAI_API_KEY. Check the nexora_settings variable library or your .env file.` with no key shown. **Restore the line.**
4. 💻 **Whitespace value.** Set `AZURE_OPENAI_DEPLOYMENT=   ` and repeat step 3 (`import app.llm.client`). **Expected:** the same error, naming `AZURE_OPENAI_DEPLOYMENT`. **Restore it.**
5. 💻 **Directory-independent.** `cd C:\` then `python -c "import sys; sys.path.insert(0, r'C:\Users\umdfaroo\Downloads\NEXORA-main\NEXORA-(Rayfin)'); import app.config as c; print(c.ENVIRONMENT)"`. **Expected:** the `.env` value. Then `cd` back to the project root.
6. 🌐 **Variable library in a job.** Needs T-005 done first. In a notebook of the home workspace (Environment `nexora_env`, `nexora_settings` attached), run `from app.runtime import RUNTIME; import app.config as c; print(RUNTIME, c.ENVIRONMENT)`. **Expected:** `fabric_job` and the library's value for the active environment.
7. 💻 **Imports work.** `python -c "import app.test_llm, app.test_fabric_sql, app.test_metadata_inventory"`. **Expected:** no output and no error.

### T-002 — One token provider for Fabric jobs, functions and local dev

1. 🌐 **Local, one sign-in per process.** `python -m app.manual_tests.t002_check`. **Expected:** the browser sign-in appears once, then the table count and `Connected successfully.` print. Run it again within an hour. **Expected:** no sign-in prompt.
2. 🌐 **Job, no prompt.** In a home-workspace notebook (needs T-005), run `from app.auth import get_token, STORAGE_SCOPE, SQL_SCOPE; print(bool(get_token(STORAGE_SCOPE)), bool(get_token(SQL_SCOPE)))`. **Expected:** `True True`, no token printed. (This is also where the `database.windows.net` raw-scope audience and the `pbi` audience for Fabric REST, recorded in `fabric-hosting.md`, get their first live check.)
3. 🌐 **Job acts as the user.** In the same notebook, list the tables of a Lakehouse you can read, then one you can't. **Expected:** the first works, the second fails with `FORBIDDEN`.
4. 🌐 **SQL in a job.** Run `SELECT 1 AS x` through `FabricSQLConnector` on the sandbox SQL endpoint. **Expected:** one row; note in `fabric-hosting.md` whether `pyodbc` (ODBC Driver 18) or the `mssql_python` fallback was used — `app/connectors/fabric_sql_connector.py:_select_driver()` picks it automatically.
5. 🌐 **UDF refuses.** In the `nexora_api` test pane (needs T-102 for a real function; until then this can be checked by calling `app.auth.get_token` from any code path reachable in the UDF host), confirm it raises `RuntimeError: Source access is not allowed in a User Data Function; run this operation as a job`.
6. 💻 **Local credential guard.** `python -c "from app.auth import get_credential; print(get_credential() is get_credential())"`. **Expected:** `True` (unchanged local behavior).

### T-003 — Environment smoke check (local and as a Fabric job)

1. 🌐 **All pass locally.** `python -m app.tools.smoke; echo $LASTEXITCODE`. **Expected:** `PASS` for `llm`, `lakehouse`, `sql` and `settings`, `SKIP control` (with `RUN_STORE=file`), exit `0`.
2. 🌐 **One failure is isolated.** `$env:FABRIC_SQL_SERVER="invalid.example.com"; python -m app.tools.smoke; echo $LASTEXITCODE`, then `Remove-Item Env:FABRIC_SQL_SERVER`. **Expected:** `FAIL sql` with a readable reason, the other checks unchanged, exit `1`.
3. 💻 **Single check.** `python -m app.tools.smoke --only llm --json`. **Expected:** JSON containing only the `llm` check. (Verified offline for `--only settings,control,lakehouse,sql`: `settings` names each setting's source, `control` skips with `RUN_STORE=file`, `lakehouse`/`sql` fail by naming the missing Fabric settings, with no secret in any output.)
4. 💻 **Bad option.** `python -m app.tools.smoke --only foo; echo $LASTEXITCODE`. **Expected:** `Invalid --only value: 'foo'. Valid checks: llm, lakehouse, sql, control, settings` and exit `2`. (Verified offline.)
5. 🌐 **In Fabric.** Needs T-005. Run `nexora_stage_runner` from the portal with `operation = smoke`. **Expected:** every check passes, `control` included, and the `settings` lines name the Variable library as the source.

### T-005 — Nexora home workspace and Fabric packaging

Everything here is new Fabric REST plumbing (`app/tools/package.py`, `app/tools/deploy_home.py`, `app/deploy/fabric_items.py`'s new methods) that has not yet been run against a real tenant — this task's own manual test is that first live run. The exact definition payloads for `UserDataFunctions` and `VariableLibrary` items are a best-effort reconstruction from public docs (see `fabric-hosting.md`); if Fabric rejects one, that's the first thing to fix.

1. 💻 **Package.** `python -m app.tools.package` twice. **Expected:** the same `wheel_hash` in `build\manifest.json` both times (verified offline: deterministic via `SOURCE_DATE_EPOCH`), and `build\config\` holds all 9 files from `config\*.yaml`.
2. 🌐 **Create.** `python -m app.tools.deploy_home --env dev --create Nexora-dev`. **Expected:** the seven items appear in the workspace (`nexora_env`, `nexora_api`, `nexora_stage_runner`, `nexora_control_db`, `nexora_control`, `nexora_settings`, `nexora_telemetry`), and the Environment shows the `nexora` wheel published. Write the workspace id into `NEXORA_HOME_WORKSPACE_ID` in `.env` afterwards for later steps.
3. 🌐 **Idempotent.** Run `python -m app.tools.deploy_home --env dev` again. **Expected:** every line says `unchanged` (the wheel line too, since nothing changed).
4. 🌐 **Smoke in Fabric.** Run `nexora_stage_runner` with `operation = smoke` (T-003 AC-5), and T-001 AC-6.
5. 🌐 **Config copied.** Open `nexora_control` → `Files/config/`. **Expected:** `roles.yaml`, `glossary.yaml` and the other 7 config files are there.
6. 💻 **Guard.** `python -m app.tools.deploy_home --env dev --create MyWorkspace; echo $LASTEXITCODE`. **Expected:** exit `2`, nothing created. (Verified offline, along with the no-`--create`-and-no-`NEXORA_HOME_WORKSPACE_ID` case and an invalid `--env` value, both also exit `2`.)

### T-004 — Test fixtures in the sandbox Lakehouse

Run the SQL checks in the Lakehouse **SQL endpoint** of the sandbox.

1. 🌐 **Tables exist.** Import `fixtures/create_test_tables.ipynb` into the sandbox workspace, attach the sandbox Lakehouse (schemas enabled, with `saleslt`) as the default, and run all cells. **Expected:** the last cell prints `All fixture checks passed.` and five tables appear under `nexora_test`.
2. 🌐 **Duplicates present.** `SELECT ProductID, COUNT(*) c FROM nexora_test.dim_product_dupes GROUP BY ProductID HAVING COUNT(*) > 1`. **Expected:** at least 3 rows (4 with the shipped notebook).
3. 🌐 **Bridge is unique.** `SELECT ProductID, PromotionID, COUNT(*) FROM nexora_test.bridge_product_promotion GROUP BY ProductID, PromotionID HAVING COUNT(*) > 1`. **Expected:** 0 rows.
4. 🌐 **Keys join to real data.** `SELECT COUNT(*) FROM nexora_test.bridge_product_promotion b LEFT JOIN saleslt.SalesLT_Product p ON b.ProductID = p.ProductID WHERE p.ProductID IS NULL`. **Expected:** `0`.
5. 🌐 **Date coverage.** `SELECT COUNT(*) FROM saleslt.SalesLT_SalesOrderHeader h LEFT JOIN nexora_test.dim_date d ON CAST(h.OrderDate AS date) = d.Date WHERE d.Date IS NULL`. **Expected:** `0`.
6. 🌐 **Rerunnable.** Run the notebook again, then repeat steps 2 and 3. **Expected:** the same results and unchanged row counts.
7. 🌐 **Visible to the harvester.** `python -m app.test_metadata_inventory nexora_test`. **Expected:** all five tables listed with their columns.

---

## M1 — Foundation

> **Fabric redevelopment (2026-10-21).** All five M1 sections below are rewritten for Fabric hosting and supersede the first build's FastAPI/Next.js-based steps. `app/api/` (FastAPI) and `rayfin-ui/` still run locally exactly as before and are not retested here; they are a local-dev fallback (see CLAUDE.md "Target hosting"), not the target.

### T-101 — Run model, state machine and run store

Steps 1–8 run offline with the file store (`RUN_STORE=file ARTIFACT_STORE=local`, the defaults) and were verified in this session. Replace `<id>` with the `run_id` printed by step 1. Steps 9–13 need a writable Fabric SQL Database and the control Lakehouse in the sandbox/home workspace (T-005); fill `FABRIC_CONTROL_DB_SERVER`, `FABRIC_CONTROL_DB_NAME`, `NEXORA_HOME_WORKSPACE_ID` and `NEXORA_CONTROL_LAKEHOUSE_ID` in `.env` first.

1. 💻 **Create.** `python -m app.cli runs create --prompt "Total sales by month"`. **Expected:** a UUID is printed, and `runs\<id>\RunManifest.json` exists with `"state": "CREATED"`, one history entry and `"active_job": null`.
2. 💻 **Rules unchanged.** Repeat step 1 (different id). `python -m app.cli runs transition <id> DEPLOYING; echo $LASTEXITCODE` → `CREATED → DEPLOYING is not allowed`, exit `1`. `python -m app.cli runs transition <id> NEEDS_CLARIFICATION --reason test` → a new history entry. `python -m app.cli runs transition <id> NEEDS_INPUT` → `resume_state: NEEDS_CLARIFICATION`.
3. 💻 **Artifact store and lease, offline.** `python -m app.manual_tests.t101_artifact_store_check`. **Expected:** `ALL CHECKS PASSED` — `write_atomic`/`read_bytes`/`exists`/`list` on `LocalArtifactStore`; 5 threads × 20 `append_line` calls land all 100 lines, none lost or interleaved; an expired lease is taken over near-instantly; a live lease blocks a second acquirer for its `wait` (here ~1s), then raises `LeaseBusy`; `runs pull` copies every file.
4. 💻 **`runs pull`.** `python -m app.cli runs pull <id> --to C:\temp\pulled-run`. **Expected:** `N files pulled into C:\temp\pulled-run`, and `RunManifest.json` is **not** among them (only artifact-store files; the manifest lives in the run store).
5. 💻 **Missing setting.** `$env:RUN_STORE="fabric_sql"; $env:FABRIC_CONTROL_DB_SERVER="x"; $env:FABRIC_CONTROL_DB_NAME=""; python -m app.cli runs list; echo $LASTEXITCODE`. **Expected:** `Missing required configuration: FABRIC_CONTROL_DB_NAME`, exit `2`, no traceback.
6. 💻 **Bad store/artifact-store name.** `$env:RUN_STORE="nope"; python -m app.cli runs list; echo $LASTEXITCODE` → `Invalid RUN_STORE: 'nope'. Valid values: file, fabric_sql`, exit `2`. `Remove-Item Env:RUN_STORE,Env:FABRIC_CONTROL_DB_SERVER,Env:FABRIC_CONTROL_DB_NAME`; then `$env:ARTIFACT_STORE="nope"; python -c "from app.store.factory import get_artifact_store; get_artifact_store()"` → `ValueError: Invalid ARTIFACT_STORE: 'nope'...`. `Remove-Item Env:ARTIFACT_STORE`.
7. 💻 **Corrupt manifest tolerated.** With the file store, `"{" | Set-Content runs\<id>\RunManifest.json` then `python -m app.cli runs list`. **Expected:** the other runs listed, a `Skipping corrupt run manifest: <id>` warning, exit `0`.
8. 💻 **Clean up.** Delete the `runs\` folder (keeps steps 1–7 from piling up).
9. 🌐 **Fabric stores, create and write.** With `RUN_STORE=fabric_sql ARTIFACT_STORE=onelake` and the settings above, `python -m app.cli runs create --prompt "x"`, then in Python `from app.store.factory import get_artifact_store; get_artifact_store().write_atomic(run_id, "probe/hello.txt", b"hi")`. **Expected:** `SELECT run_id, state FROM dbo.nexora_runs` returns the run, and `nexora_control` → `Files/runs/<run_id>/probe/hello.txt` exists.
10. 🌐 **Fabric append safety.** In a home-workspace notebook, start 5 threads each calling `get_artifact_store().append_line(run_id, "probe/log.jsonl", ...)` 20 times. **Expected:** 100 lines, none lost (uses the OneLake lock-file lease; not yet run against a live tenant — see fabric-hosting.md).
11. 🌐 **Same CLI rules against the Fabric SQL store.** Repeat step 2 with `RUN_STORE=fabric_sql`. **Expected:** identical results, and `state` in `dbo.nexora_runs` matches `runs show`.
12. 🌐 **Unreachable database.** `$env:FABRIC_CONTROL_DB_SERVER="invalid.example.com"; python -m app.cli runs list; echo $LASTEXITCODE`. **Expected:** a readable "Cannot connect" message, exit `1`. (This needs a real control-DB connection string configured first — the local interactive credential still has to resolve, which this step also exercises; see T-002.) **Restore it.**
13. 🌐 **Clean up.** `DELETE FROM dbo.nexora_runs WHERE run_id = '<id>'` and delete the matching `Files/runs/<id>/` folder in `nexora_control`.

### T-102 — Fabric orchestration surface (User Data Functions and stage jobs)

Steps 1–8 run offline (`RUN_STORE=file ARTIFACT_STORE=local`) and were verified in this session. They exercise `app.fabric.dispatch.dispatch()` and `app.fabric.job` directly, standing in for the real `nexora_api`/`nexora_stage_runner` transports (steps 9+). A fake `PRINCIPAL = {"oid": "...", "upn": "tester@contoso.com", "name": "Tester"}` stands in for a UDF invocation context throughout.

1. 💻 **Offline dispatcher checks.** `python -m app.manual_tests.t102_dispatch_offline_checks`. **Expected:** `ALL CHECKS PASSED` — health with no principal, create (`201`), read and list, `422 VALIDATION_ERROR` on an empty prompt, `404 RUN_NOT_FOUND` on an unknown id, `400 BAD_REQUEST` when a `job:` operation (`PUT /runs/{id}/scope`) is called the UDF way (`as_job=False`), a full job round trip (reserve → `RUN_BUSY` on a second reservation → `404` before the result exists → `run_job` → result readable → `active_job` settles → `next_action` stops saying "Wait for …" → a new reservation is accepted again).
2. 💻 **Health.** In Python: `from app.fabric.dispatch import dispatch; dispatch("GET", "/health", {}, None, None, as_job=False)`. **Expected:** `{"status": 200, "body": {"status": "ok", "app": "Nexora", "version": "..."}, "headers": None}` — works with **no** principal.
3. 💻 **Create.** `dispatch("POST", "/runs", {}, {"prompt": "Sales by product category"}, PRINCIPAL, as_job=False)`. **Expected:** `status: 201`, a `run_id`, `state: CREATED`, `next_action: "Compile requirement"`, `active_job: null`.
4. 💻 **Job round trip by hand.** `from app.fabric.job import reserve_job, run_job; from app.store.factory import get_run_store; store = get_run_store(); job_id, run = reserve_job(store, run_id, "POST /runs/{id}/compile", {}, PRINCIPAL)`, then `run_job(job_id, run_id, "POST /runs/{id}/compile", {})`. **Expected:** `run.active_job.status == "queued"` right after `reserve_job`; `run_job`'s return has `status`/`body`; `GET /runs/{id}/jobs/{job_id}` (via `dispatch`) then returns the same result; `GET /runs/{id}`'s `active_job.status` is `succeeded` or `failed`, never stuck at `queued`/`running`.
5. 💻 **One job at a time.** While the run from step 4 still shows `active_job.status: queued` (reserve but don't run `run_job` yet), call `reserve_job` again. **Expected:** `ApiError` with `.code == "RUN_BUSY"` and `.details["active_job"]`.
6. 💻 **Clean up.** Delete the `runs\` folder.
7. 🌐 **Health through the real UDF.** In the Fabric portal, open `nexora_api` (Develop mode or the test pane) and test `call("GET", "/health", {}, None)`. **Expected:** `status: 200`, `runtime` not applicable (the body has no `runtime` key currently — if CONTRACTS.md is amended to add one, update this step), `app: "Nexora"`.
8. 🌐 **Create through the real UDF.** Test `call("POST", "/runs", {}, {"prompt": "Sales by product category"})`. **Expected:** `status: 201` with a `run_id`, `state: CREATED`. Then `call("GET", "/runs/{run_id}")` and `call("GET", "/runs")` show it. **Known gap (see CLAUDE.md "Target hosting"):** this works for a fresh run; once a job has written a contract/inventory, reading it back through this UDF may fail until the stage services are retrofitted to `ArtifactStore` — note exactly what breaks, as a finding, not something to fix in this pass.
9. 🌐 **Validation and not found.** `call("POST", "/runs", {}, {"prompt": ""})` → `422` with the envelope; `call("GET", "/runs/00000000-0000-0000-0000-000000000000")` → `404 RUN_NOT_FOUND`.
10. 🌐 **Job round trip for real.** `call("POST", "/runs/{run_id}/jobs", {}, {"operation": "POST /runs/{id}/compile", "body": {}})` → `202` with a `job_id`. Start `nexora_stage_runner` from the portal with `job_id`, `run_id`, `operation = "POST /runs/{id}/compile"`, `payload = "{}"`. **Expected:** `active_job` goes `queued → running → succeeded`/`failed`, `call("GET", "/runs/{run_id}/jobs/{job_id}")` returns the compile response, and the state moves per T-202 (or the job fails cleanly if `AZURE_OPENAI_*` isn't configured for the job).
11. 🌐 **One job at a time, for real.** While the job from step 10 is still running, `call("POST", "/runs/{run_id}/jobs", ...)` again. **Expected:** `409 RUN_BUSY`.
12. 🌐 **Crash handled.** Cancel the running job from the Monitoring hub, then `call("GET", "/runs/{run_id}")`. **Expected:** `active_job.status: failed` with `JOB_FAILED` and the `job_instance_id` (needs `NEXORA_STAGE_RUNNER_ID` set so `reconcile_active_job` can ask the Job Scheduler API), and `next_action` no longer says "Wait for …".
13. 🌐 **Shared store.** Locally, with `RUN_STORE=fabric_sql ARTIFACT_STORE=onelake` pointed at the same home workspace, `python -m app.cli runs list`. **Expected:** it shows the runs created through `nexora_api` in steps 8/10.

### T-103 — Workspace and Lakehouse selection (UI pickers, scope bound by a job)

The backend half needed no new code: `PUT /runs/{id}/scope` was already `app.workflow.scope.bind_scope` (unchanged), now behind the `job:` host in the dispatcher table. These steps confirm the wiring; the pickers themselves are tested as part of T-104 (they're UI-only, no Nexora operation).

1. 💻 **Already covered offline.** `app.manual_tests.t102_dispatch_offline_checks` step 1's "job op via call() -> 400 BAD_REQUEST" check uses `PUT /runs/{id}/scope` for exactly this. No separate offline script is needed; `bind_scope`'s own logic is unchanged from the first build.
2. 🌐 **Bind scope as a job.** Reserve and run `PUT /runs/{id}/scope` (`{"workspace_id": "...", "lakehouse_id": "...", "schemas": ["saleslt"]}`) via `nexora_stage_runner`, then `GET /runs/{id}`. **Expected:** `scope` shows `workspace_name`, `lakehouse_name`, `schemas` and `sql_endpoint`.
3. 🌐 **Invalid id refused.** Same with a random GUID as `workspace_id`. **Expected:** the job's result is `403 FORBIDDEN`; the run's `scope` is unchanged.
4. 🌐 **Bad schema.** `"schemas": ["does_not_exist"]`. **Expected:** `404 SCHEMA_NOT_FOUND` naming it.
5. 🌐 **Locked after harvest.** After T-301's harvest runs once, try to reserve the scope job again. **Expected:** the operation's own precondition fails inside the job (`409 SCOPE_LOCKED`), since reservation itself doesn't invoke `bind_scope` (see `app/fabric/job.py`'s docstring) — note exactly what reservation vs. execution each do if this differs from the task's stated "checked at reservation."
6. 🌐 **Pickers match the portal.** Covered by T-104 AC-1 (the New run page's workspace/Lakehouse dropdowns).

### T-104 — Rayfin workload shell in Fabric

Verified in this session (offline, no browser): `cd rayfin-workload; npm install; npm run typecheck; npm test`. **Expected:** `tsc --noEmit` reports nothing, and all 60 ported display-rule tests pass (`node --test`). This is the full extent of what's testable without the real Fabric Extensibility Toolkit Starter-Kit and a live tenant — see `rayfin-workload/README.md`'s "What's not included" before attempting the steps below, since they need this project dropped into that Starter-Kit first.

1. 🌐 **Create in Fabric.** In the Fabric portal (workload in developer mode), open Rayfin → **New run**, enter a prompt, choose a workspace and Lakehouse, and submit. **Expected:** the run page opens with state `CREATED`, the chosen scope (bound by the scope job), and "Compile requirement" as the next action.
2. 🌐 **Listed.** Open the Rayfin home. **Expected:** the new run is at the top.
3. 🌐 **One-screen status.** On the run page at 1366×768. **Expected:** the state, next action, job progress and timeline are visible without scrolling.
4. 🌐 **Function down.** Remove/blank `NEXORA_API_CALL_FUNCTION_URL` and submit a new run. **Expected:** an error banner, and the prompt text is still there.
5. 🌐 **Validation shown.** Submit an empty prompt. **Expected:** an inline message on the prompt field.
6. 💻 **Display rules intact.** `npm test` in `rayfin-workload/`. **Expected:** the 60 ported tests pass unchanged (verified in this session).

### T-105 — Fabric identity and attribution

1. 💻 **UDF context parsing and job identity mismatch, offline.** `python -m app.manual_tests.t105_identity_offline_checks`. **Expected:** `ALL CHECKS PASSED` — `principal_from_udf_context` handles a dict-shaped `executing_user`, an attribute-shaped one with no `PreferredUsername` (falls back to `Oid`), `None`, and a context with no `executing_user` (never raises); a job reserved by one user and run (mocked) as another gets `403 FORBIDDEN` and writes **no** result file, leaving `active_job` untouched at `queued`; the same job run as the reserving user proceeds normally.
2. 🌐 **Signed in by Fabric.** Open Rayfin in the Fabric portal in a private window. **Expected:** the Fabric sign-in only, no separate Rayfin login (there is none in `rayfin-workload`).
3. 🌐 **Attribution.** Create a run. **Expected:** `created_by` is set and the first history entry's `actor` is your UPN (`python -m app.cli runs show <id>` to check, or the run page once T-104's display shows it).
4. 🌐 **Anonymous refused.** Invoke `nexora_api`'s `call("GET", "/runs")` without a user context (for example from a service principal with no delegated user). **Expected:** `401 UNAUTHENTICATED`. `call("GET", "/health")` still returns `200`.
5. 🌐 **Per-user resources.** Sign in as a second test user with access to fewer workspaces. **Expected:** the pickers (T-104's `fabricRest.ts`) show only that user's workspaces; binding one of the first user's workspaces fails with `403`.
6. 🌐 **Job identity for real.** Reserve a job as user A (`POST /runs/{id}/jobs`), then start `nexora_stage_runner` under user B's credentials (or edit the parameters to run as a different identity if your tooling allows it). **Expected:** `403 FORBIDDEN`, written to nothing; `GET /runs/{id}/jobs/{job_id}` still `404`; the run unchanged.
7. 🌐 **M1 exit.** As a signed-in user, create a run with a scope and open its page in the Fabric portal. **Expected:** this works end to end without the CLI or a test pane.

---

## M2 — Requirement control

### T-201 — BusinessContract schema

1. 💻 **Valid sample.** `python -m app.cli contract validate samples/contract_valid.yaml; echo $LASTEXITCODE`. **Expected:** `VALID` and `0`.
2. 💻 **Missing field.** `Copy-Item samples\contract_valid.yaml $env:TEMP\c2.yaml`, open the copy, set `status: approved` on the first line that says `status: draft` (the contract's own status, near the top), and delete the KPI's `formula_intent:` line. Run `python -m app.cli contract validate $env:TEMP\c2.yaml; echo $LASTEXITCODE`. **Expected:** `INVALID`, a line starting `kpis.0.formula_intent:` and exit `1`. Put the line back and the file is `VALID` again.
3. 💻 **Blocking question prevents approval.** In the copy (contract `status: approved`, `formula_intent` restored), replace `open_questions: []` with:
   ```yaml
   open_questions:
   - id: Q1
     text: How is Revenue defined?
     blocking: true
     source: rule
     status: open
   ```
   **Expected:** `INVALID` with a line naming `open_questions.0` and `Q1`. Change `status: open` to `status: answered` and it is `VALID`. With contract `status: draft` the open question is allowed.
4. 💻 **Round trip.** `python -c "from app.contracts import business_contract as b; print(b.save(b.load('samples/contract_valid.yaml'), r'$env:TEMP
t.yaml'))"` then `fc.exe /b samples\contract_valid.yaml $env:TEMP
t.yaml`. **Expected:** a `sha256:…` line, then `FC: no differences encountered`.
5. 💻 **Schema export.** `python -m app.cli contract schema --output $env:TEMP\schema.json`, then `python -c "import json,os; d=json.load(open(os.environ['TEMP']+'/schema.json')); print('kpis' in d['properties'])"`. **Expected:** `True`. (`python -m app.cli contract schema` prints the same JSON; with PowerShell's `>` the file would be UTF-16, which is why `--output` exists.) Compare with the committed file: `python -m app.manual_tests.t201_offline_checks` reports it as current.
6. 💻 **Edge cases.** Make a copy each time. (a) Rename `kpis:` to `kpi:`. **Expected:** `kpi: extra field not permitted`. (b) Add `- name: net revenue` right under `kpis:`. **Expected:** a `kpis.1.name` line that also names `kpis.0.name`. (c) Set `schema_version: 2`. **Expected:** `unsupported schema_version 2`. (d) Empty the file. **Expected:** `empty document`. (e) Break the YAML, for example a lone `[` line. **Expected:** a message with `line` and `column`. (f) A path that doesn't exist. **Expected:** `File not found: …`, exit `2`.
7. 💻 **Offline checks.** `python -m app.manual_tests.t201_offline_checks`. **Expected:** `All checks passed.` (38 checks covering all of the above, determinism, UTF-8/LF output, hash and the committed schema file).
8. 💻 **Clean up.** `Remove-Item $env:TEMP\c2.yaml, $env:TEMP
t.yaml, $env:TEMP\schema.json -ErrorAction SilentlyContinue`.

---

### T-202 — Requirement Compiler with ambiguity detection

> **Fabric re-test (2026-10-21).** Logic unchanged; only the transport and the config source moved. The dispatcher wiring was checked against `CONTRACTS.md#fabric-operations` (`compile` is a `job:` operation, hosted correctly in `app/fabric/dispatch.py`), the node-path log line (`app/graphs/base.py:run_graph`) already fires inside a job's stdout, and `config/critical_terms.yaml` now reads from `Files/config/` of `nexora_control` in a job (`app/config_files.py`, wired into `app/compiler/critical_terms.py`) instead of a local checkout — local development and every offline check are unaffected (they never set `NEXORA_HOME_WORKSPACE_ID`/`NEXORA_CONTROL_LAKEHOUSE_ID` together with `RUNTIME=fabric_job`, so they keep reading the local file exactly as before). `uvicorn`/Swagger steps below are the local-dev fallback (see CLAUDE.md "Target hosting"); the 🌐 steps are the Fabric equivalents.

1. 💻 **Offline checks.** `python -m app.manual_tests.t202_offline_checks`. **Expected:** `All checks passed.` (verified in this session — 80+ checks with a fake LLM: the rules, the retry and the 2-attempt limit, `LLM_INVALID_OUTPUT` with the raw output saved under `logs\`, timeout and content-filter errors, versioning, locked facts, state changes and the log line, including "a bad critical_terms.yaml stops the API at startup, naming the file").
2. 🌐 **Ambiguous Revenue stops, as a job.** In the `nexora_api` test pane: `call("POST", "/runs", {}, {"prompt": "Show revenue by month for 2024"})`, note the `run_id`, then `call("POST", "/runs/{run_id}/jobs", {}, {"operation": "POST /runs/{id}/compile", "body": {}})`. Start `nexora_stage_runner` with the returned `job_id`, the `run_id` and `operation = "POST /runs/{id}/compile"`. **Expected:** once the job finishes, `call("GET", "/runs/{run_id}/jobs/{job_id}")` returns `run.state: NEEDS_CLARIFICATION`, `blocking_questions: 1`, and `run.open_questions` has `{"id": "RULE-REVENUE", "text": "How is Revenue defined?", "blocking": true, "source": "rule", …}`. `python -m app.cli runs pull <run_id>` then `contract validate` on the pulled `BusinessContract.yaml` prints `VALID`.
3. 🌐 **Explicit definition passes.** Repeat step 2 with the prompt `Show Net Revenue by month for 2024. Net Revenue = SUM(LineTotal) excluding cancelled orders, in USD.`. **Expected:** `run.state: CREATED`, `blocking_questions: 0`, no Revenue question, and the KPI `formula_intent` mentions `LineTotal` and the exclusion.
4. 🌐 **Wrong state.** Create a run, `python -m app.cli runs transition <run_id> REQUIREMENT_APPROVED` (CLI, against the Fabric stores), then repeat the compile job. **Expected:** the job result is `409 WRONG_STATE`, and no `contracts/` files were written (`runs pull <run_id>` shows none).
5. 🌐 **Config read from Files/config/.** In `nexora_control` → `Files/config/`, rename `critical_terms.yaml` temporarily, then run a compile job. **Expected:** the job's result is `500 CONFIG_ERROR` naming `critical_terms.yaml`; the notebook's own log shows the same message (never the file's content). Rename it back and the next compile job succeeds again.
6. 🌐 **Node-path log line.** Open the `nexora_stage_runner` run from step 2 in the Monitoring hub (or the notebook's own output). **Expected:** a line `stage=compile run_id=<run_id> node_path=llm_compile -> validate_schema -> apply_critical_terms -> apply_locked_facts`.

**Not run by Claude:** steps 2–6, which need a live Fabric tenant, a deployed `nexora_api`/`nexora_stage_runner` and the real model. Claude verified step 1 and, in earlier local-dev testing recorded in this file's history, the equivalent FastAPI-hosted behavior.

---

### T-203 — Clarification loop

> **Fabric re-test (2026-10-21).** Logic unchanged; `…/answer` is a `job:` operation (it recompiles) and `…/dismiss`/`PUT …/contract/kpis/{name}` are `udf:`, matching `CONTRACTS.md#fabric-operations` and confirmed against `app/fabric/dispatch.py`. No config file or OBO use in this task's own modules.

1. 💻 **Offline checks.** `python -m app.manual_tests.t203_offline_checks`. **Expected:** `All checks passed.` (verified in this session — fake LLM: answer, locked fact, state change, locked fact surviving a recompile and a contradicting model, dismiss rules, re-answer, stale and locked edits, 404/409/422 cases).
2. 🌐 **Answer, as a job.** On a run compiled per T-202 step 2, reserve and run `POST /runs/{id}/questions/RULE-REVENUE/answer` with `{"answer": "Net Revenue = SUM(LineTotal) excluding cancelled orders"}` through `nexora_stage_runner`. **Expected:** the job result's `contract` shows `RULE-REVENUE` `status: answered`, a locked fact `LF-RULE-REVENUE`, and the run moves to `CREATED`.
3. 🌐 **Dismiss and KPI edit, as udf calls.** `call("POST", "/runs/{run_id}/questions/{qid}/dismiss", {}, {"reason": "Out of scope"})` on a non-blocking question. **Expected:** `200`, the question is `dismissed`. `call("PUT", "/runs/{run_id}/contract/kpis/Net Revenue", {}, {"currency": "EUR", "expected_hash": "<hash>"})` with a stale hash gives `409 STALE_VERSION`; with the current hash, `200` and a new contract version.
4. 🌐 **Edit after approval.** Once the contract is `approved` (T-204), repeat step 3's PUT. **Expected:** `409 CONTRACT_LOCKED`.

**Not run by Claude:** steps 2–4, which need a live Fabric tenant and the real model. Claude verified step 1.

---

### T-204 — Approval service and Gate G1

> **Fabric re-test (2026-10-21).** Logic unchanged. `POST /runs/{id}/gates/G1/check`, `GET`/`POST /runs/{id}/approvals` (ACCEPT, REJECT, EDIT, EXPLAIN) are `udf:`; `REGENERATE` is `job:` (`_approve_host` in `app/fabric/dispatch.py` picks the host from the body's `action`, matching `CONTRACTS.md#fabric-operations`). `config/roles.yaml` now reads from `Files/config/roles.yaml` of `nexora_control` in a job, the same way as T-202's `critical_terms.yaml` (`app/config_files.py`, wired into `app/workflow/roles.py`); a UDF and local development keep reading the local file. A missing or invalid roles file is `500 CONFIG_ERROR` on the first call that needs it (there is no API startup check in Fabric).

1. 💻 **Offline checks.** `python -m app.manual_tests.t204_offline_checks`. **Expected:** `All checks passed.` (verified in this session — fake LLM and a temporary roles file: G1 checks, accept, locking, roles, reject, explain, edit, regenerate, and the 403/404/409/422 cases, including "a missing roles.yaml is 500 CONFIG_ERROR naming the file").
2. 🌐 **Role setup.** In `nexora_control` → `Files/config/roles.yaml`, add your own UPN to `business_owner` (or edit `config/roles.yaml` and run `python -m app.tools.deploy_home` to republish it). Roles are read on every call, so no redeploy of `nexora_api` is needed after editing the file in place.
3. 🌐 **Gate blocks, then approves.** On a run with the Revenue question still open, `call("POST", "/runs/{run_id}/approvals", {}, {"artifact": "BusinessContract", "action": "ACCEPT", "expected_hash": "<hash>"})`. **Expected:** `409 GATE_FAILED` naming `G1.2`. Answer the question (T-203), then repeat `ACCEPT`. **Expected:** `200`, `run.state: REQUIREMENT_APPROVED`, and `call("GET", "/runs/{run_id}/approvals")` lists the record.
4. 🌐 **Regenerate, as a job.** `call("POST", "/runs/{run_id}/jobs", {}, {"operation": "POST /runs/{id}/approvals", "body": {"artifact": "BusinessContract", "action": "REGENERATE", "expected_hash": "<hash>", "comment": "Use fiscal months"}})`, then run it via `nexora_stage_runner`. **Expected:** the job result recompiles, keeping the locked facts, and writes a `REGENERATE` approval record.
5. 🌐 **Bad roles file.** Rename `Files/config/roles.yaml` in `nexora_control` temporarily, then call an approval action. **Expected:** `500 CONFIG_ERROR` naming `roles.yaml`. Rename it back.

**Not run by Claude:** steps 2–5, which need a live Fabric tenant and the real model. Claude verified step 1.

---

### T-205 — Clarification and contract review UI

> **Fabric rework (2026-10-21).** The backend is T-202/T-203/T-204, unchanged. The UI is new: an **Overview** tab (`rayfin-workload/src/pages/RunDetailPage.tsx`, now including the ported `ClarificationPanel`) and a **Contract** tab (`rayfin-workload/src/pages/ContractPage.tsx`) on the **Nexora Run** item editor (`src/items/NexoraRunItem/NexoraRunItemEditor.tsx`, a `TabList`), replacing `rayfin-ui`'s `/runs/[id]` page and `/runs/[id]/contract`. `src/lib/review.ts` (display rules) and `src/components/{KpiCard,LongText}.tsx` were ported unchanged from `rayfin-ui/`; `src/lib/api.ts` gained `dismissQuestion`, `checkG1`, `listApprovals` and `approve` (the last picks `call` or `runJob` by `action`, matching T-204's dispatcher host). There is no dev server for `rayfin-workload` yet (see its README's "What's not included"), so the steps below need this project dropped into the real Extensibility Toolkit Starter-Kit first.

1. 💻 **Typecheck and display-rule tests.** `cd rayfin-workload; npm run typecheck; npm test`. **Expected:** `tsc --noEmit` reports nothing, and all 60 tests pass (`node --test`), unchanged by this task (verified in this session).
2. 🌐 **Ambiguous Revenue end to end.** In Rayfin (Fabric portal), **New run** with `Show revenue by month for 2024`. On the **Overview** tab click **Compile** in the Clarification panel. **Expected:** `JobProgress` shows "Queued" then "Running · …s" while the compile job runs; afterwards the panel lists a **Blocking** question "How is Revenue defined?". Type the answer and **Submit answer**. **Expected:** the question disappears and the state becomes `CREATED`. Click **Contract review** (switches to the **Contract** tab), then **Accept**. **Expected:** the Overview tab's timeline shows `CREATED → NEEDS_CLARIFICATION → CREATED → REQUIREMENT_APPROVED`, and the Contract tab's **Approval history** shows an `ACCEPT` entry with your UPN and the time.
3. 🌐 **Blocked approval is explained.** Create and compile another run with the same prompt, don't answer, open the **Contract** tab. **Expected:** **Accept** is disabled with "Accept is unavailable: Gate G1 failed: G1.2, …", and the G1 checklist shows `G1.2` failing.
4. 🌐 **Stale version.** Open the same run's Contract tab in two browser tabs. In tab 1, edit a KPI and Save; in tab 2 (not reloaded), click Accept or Regenerate. **Expected:** "The contract changed. Reload to see the latest version." and nothing is approved.
5. 🌐 **M2 exit.** Using only Rayfin, produce an approved, locked contract (step 2). **Expected:** the Contract tab shows "Approved and locked" with Edit/Accept/Reject/Regenerate gone (Explain remains).

**Not run by Claude:** steps 2–5, which need a live Fabric tenant, the Rayfin workload deployed into the real Starter-Kit, and the real model. Claude verified step 1.

---

## M3 — Data understanding

### T-301 — Metadata Harvester per run

> **Fabric re-test (2026-10-21).** Logic unchanged. `POST /runs/{id}/harvest` is `job:`; `GET /runs/{id}/inventory` is `udf:` (`CONTRACTS.md#fabric-operations`, confirmed against `app/fabric/dispatch.py`). The OneLake table API is called as the job's own user through `app.auth.get_token` (T-002; `app/fabric/deps.py:connector_factory`), not OBO — confirmed: `FabricLakehouseConnector.for_scope` defaults its `token_provider` to `get_token(STORAGE_SCOPE)` and the dispatcher never passes another one. **Known gap (see CLAUDE.md "Target hosting"):** `harvest_stage.py` still reads and writes `MetadataInventory.json` through a local `Path` (`app.fabric.deps.runs_root()`), not T-101's `ArtifactStore`. Writing from `_harvest` works inside a job (the attached Lakehouse's `Files/` mount), but `GET /runs/{id}/inventory` runs in a UDF, which has no such mount — step 3 below is exactly this gap and is expected to fail until a later retrofit; it is a finding, not something fixed in this pass.

1. 💻 **Offline checks.** `python -m app.manual_tests.t301_offline_checks`. **Expected:** `All checks passed.` (verified in this session — fake Lakehouse: table paging, per-table errors, `NO_COLUMNS`, Unicode names, 520 tables with progress logs, versioning, schema override, wrong state, missing scope, re-harvest from `DATA_PROFILED`, 0 tables warning, listing failures).
2. 🌐 **Harvest, as a job.** On a run bound to `saleslt` (T-103): `call("POST", "/runs/{run_A}/jobs", {}, {"operation": "POST /runs/{id}/harvest", "body": {}})` → `202` with a `job_id`. Start `nexora_stage_runner` with `job_id`, `run_id = run_A`, `operation = "POST /runs/{id}/harvest"`, `payload = "{}"`. **Expected:** once the job settles, `call("GET", "/runs/{run_A}/jobs/{job_id}")` returns `200` with `summary` (`tables`, `columns`, `errors`) and `artifact` (`name: MetadataInventory`, `version: 1`, a `sha256:` hash). `python -m app.cli runs pull <run_A>` then `runs/<run_A>/MetadataInventory.json` lists every `saleslt` table and the `summary` counts match it.
3. 🌐 **Read through the udf.** `call("GET", "/runs/{run_A}/inventory")`. **Expected:** `200` with the same content as the pulled file. **If this instead fails** (empty body, a filesystem error, or `404` on a run that was just harvested), that confirms the known gap above — record exactly what broke under "Not run by Claude" below rather than fixing it here.
4. 🌐 **Matches the portal.** Pick three tables from the pulled file and compare their columns, types and nullability with the Lakehouse explorer in Fabric. **Expected:** identical.
5. 🌐 **Not hardcoded.** On a run bound with an empty schema list, run the harvest job with `body = {"schemas": ["nexora_test"]}`. **Expected:** the five fixture tables from T-004, and `scope.schemas` in the pulled file is `["nexora_test"]`.
6. 🌐 **Wrong state.** On a run still `CREATED`, reserve `POST /runs/{id}/harvest` as a job. **Expected:** the dispatcher's cheap precondition check at reservation time, or the job result if it reserves anyway, is `409 WRONG_STATE` (note which, per `app/fabric/job.py`'s docstring). A harvest job with `body = {"schemas": ["no_such_schema"]}` on a bound run gives `404 SCHEMA_NOT_FOUND`, and the inventory file is unchanged.
7. 🌐 **Versioning.** Run the harvest job on run A again. **Expected:** the result's `artifact.version` is `2`, `call("GET", "/runs/{run_A}/artifacts")` shows `MetadataInventory` at version 2 with a new hash, and `python -m app.cli runs pull <run_A>` shows `inventories/MetadataInventory.v1.json` and `.v2.json`.

**Not run by Claude:** steps 2–7, which need a live Fabric tenant and the real Lakehouse. Claude verified step 1.

---

### T-302 — Generic data profiler

> **Fabric re-test (2026-10-21).** Logic unchanged. `POST /runs/{id}/profile` is `job:` (`CONTRACTS.md#fabric-operations`, confirmed against `app/fabric/dispatch.py`). The SQL endpoint is queried as the job's own user through `app.auth.get_token` (T-002; `app/fabric/deps.py:sql_connector_factory`), not OBO — confirmed: `FabricSQLConnector.for_scope` defaults its `token_provider` to `get_token(SQL_SCOPE)`. The 120s statement timeout applies per query inside the job. Same known `ArtifactStore` gap as T-301 (`profile_stage.py` still reads/writes the inventory through a local `Path`); profiling itself is a job, so it is unaffected — only a later `GET /runs/{id}/inventory` read of the profiled file (T-301's udf) can show it.

1. 💻 **Offline checks.** `python -m app.manual_tests.t302_offline_checks`. **Expected:** `All checks passed.` (verified in this session — a fake SQL endpoint over in-memory tables: counts, uniqueness, duplicate count, min and max, freshness, empty and all-null cases, removed and timed-out tables, binary columns, the 10M-row approximate path, no string values stored, SELECT-only SQL, and the 404/409/422/400 cases).
2. 🌐 **Row count matches, as a job.** On run A (harvested for `saleslt`, contract `Show total sales by product`): reserve and run `POST /runs/{id}/profile` with `body = {"tables": ["saleslt.SalesLT_Product"]}` through `nexora_stage_runner`. **Expected:** the job result is `{"profiled": n, "warnings": n, "errors": 0}`. In the SQL endpoint run `SELECT COUNT(*) FROM saleslt.SalesLT_Product`; `call("GET", "/runs/{run_A}/inventory")` (or the pulled file, per T-301 step 3's gap) shows the same number as `row_count`.
3. 🌐 **Unique key.** In the same profile, `columns.ProductID`. **Expected:** `is_unique: true` and `distinct_count` equal to `row_count`.
4. 🌐 **Duplicates detected.** On run B (harvested for `saleslt` and `nexora_test`), run the profile job with `body = {"tables": ["nexora_test.dim_product_dupes"]}`. **Expected:** `columns.ProductID.is_unique: false`, table `status: warning`, and a warning "ProductID is not unique (n duplicate values)". `SELECT ProductID, COUNT(*) c FROM nexora_test.dim_product_dupes GROUP BY ProductID HAVING COUNT(*) > 1` returns `n` rows (at least 3, per T-004 AC-2).
5. 🌐 **Date coverage.** Pick a date column (for example `ModifiedDate` of `SalesLT_Product`) and run `SELECT MIN(ModifiedDate), MAX(ModifiedDate) FROM saleslt.SalesLT_Product`. **Expected:** equal to that column's `min`/`max` in the profile (UTC, ISO format), and `freshness.max_value` equals the freshness column's `max`.
6. 🌐 **No raw strings.** Pick a known product name (`SELECT TOP 1 Name FROM saleslt.SalesLT_Product`) and search the pulled `MetadataInventory.json` for it. **Expected:** no match; string columns have counts only, no `min`/`max`.
7. 🌐 **Edge cases.** A profile job with `body = {"tables": ["saleslt.NoSuchTable"]}` gives `404 NOT_FOUND`; profiling before any harvest gives `404 NOT_FOUND`; profiling a `CREATED` run gives `409 WRONG_STATE`. Re-harvest run A (T-301): **Expected:** the inventory is version 2 and every `profile` is `null` again.

**Not run by Claude:** steps 2–7, which need a live Fabric tenant and the real Lakehouse. Claude verified step 1.

---

### T-303 — Relationship evidence

> **Fabric re-test (2026-10-21).** Logic unchanged. `POST /runs/{id}/relationships/analyze` is `job:`; `GET /runs/{id}/relationships` is `udf:` (confirmed against `app/fabric/dispatch.py`). The SQL endpoint is queried as the job's own user (T-002, same as T-302); the LLM call (candidate suggestions) also runs only inside the job. AC-1's comparison script, `python -m app.test_product_mapping`, still runs locally against the live SQL endpoint. Same known `ArtifactStore` gap as T-301 for a `GET /runs/{id}/relationships` udf read of a just-analyzed run.

1. 💻 **Offline checks.** `python -m app.manual_tests.t303_offline_checks`. **Expected:** `All checks passed.` (verified in this session — fake SQL endpoint and model: heuristics, LLM and user candidates, the unique/duplicate/null/orphan/overlap numbers, high/medium/low, one_to_one/many_to_one/many_to_many, rejections for hallucinated table and column, self-relationship and missing profile, type mismatch with no join query, names-and-types-only model payload, model failure degrading to a warning, replaced evidence, cleared on re-harvest, and the 404/409/422/400 cases).
2. 🌐 **Real join measured, as a job.** On run A (harvested and profiled for `saleslt`): reserve and run `POST /runs/{id}/relationships/analyze` with `body = {}` through `nexora_stage_runner`. **Expected:** the job result is `{"candidates": n, "high": n, "medium": n, "low": n, "rejected": n, "warnings": []}`. `call("GET", "/runs/{run_A}/relationships")` (or the pulled inventory, per T-301 step 3's gap) has an entry from `saleslt.SalesLT_SalesOrderDetail.ProductID` to `saleslt.SalesLT_Product.ProductID` with `cardinality: many_to_one`, `confidence: high`, `source: heuristic`. Run `python -m app.test_product_mapping` locally: its `Detail rows`/`Matched detail rows` give the orphan rate, `(detail_rows - matched_rows) / detail_rows * 100`, equal to `orphan_rate`.
3. 🌐 **Bad join is low confidence.** Run the analyze job again with `body = {"candidates": [{"from": {"table": "saleslt.SalesLT_SalesOrderDetail", "column": "ProductID"}, "to": {"table": "saleslt.SalesLT_Customer", "column": "CustomerID"}}]}`. **Expected:** that entry has `source: user`, `confidence: low` and a small `overlap_pct`.
4. 🌐 **Invalid relationship evidence.** On run B (includes `nexora_test.dim_product_dupes`), run the analyze job with `body = {}`, then look for entries whose `to.table` is `nexora_test.dim_product_dupes`. **Expected:** `cardinality: many_to_many`, `confidence: low`, a note "The target column is not unique, so this can't be many-to-one".
5. 🌐 **Hallucinated column rejected.** Inject a user candidate with a column that doesn't exist (`body = {"candidates": [{"from": {...}, "to": {"table": "saleslt.SalesLT_Product", "column": "NoSuchColumn"}}]}`). **Expected:** `status: rejected`, `rejected_reason: "column not in inventory"`. (The model inventing one is exercised by the offline check, step 1.)
6. 🌐 **Stored and cleared.** Re-harvest run A (T-301). **Expected:** `call("GET", "/runs/{run_A}/relationships")` is `[]`.

**Not run by Claude:** steps 2–6, which need a live Fabric tenant, the real model and the real Lakehouse. Claude verified step 1.

---

### T-304 — Gate G2 and inventory UI

> **Fabric rework (2026-10-21).** The backend (G2, exceptions, data gaps, the `MetadataInventory` approval) is T-301/T-302/T-303's, unchanged, and was confirmed wired through the dispatcher: `POST /runs/{id}/gates/G2/check`, `POST /runs/{id}/exceptions` and `POST /runs/{id}/data-gaps` are `udf:`; `POST /runs/{id}/approvals` on `MetadataInventory` (`ACCEPT` only — `REJECT`/`EDIT`/`EXPLAIN`/`REGENERATE` give `422`, matching T-304's constraint) is `udf:` too, since only `REGENERATE` is ever `job:` (`_approve_host`). The UI is new: an **Inventory** tab (`rayfin-workload/src/pages/InventoryPage.tsx`, with `src/components/InventoryPanels.tsx` for the table and relationship rows) added to the **Nexora Run** item editor (`src/items/NexoraRunItem/NexoraRunItemEditor.tsx`), replacing `rayfin-ui`'s `/runs/[id]/inventory`. `src/lib/inventory.ts` and its tests were already ported in T-104 and are unchanged here; `src/lib/api.ts` gained `getInventory`, `getRelationships`, `harvest`, `profile`, `analyzeRelationships` (jobs, via `runJob`), `checkG2`, `approveException` and `raiseDataGap`. There is no dev server for `rayfin-workload` yet (see its README), so the 🌐 steps need this project dropped into the real Extensibility Toolkit Starter-Kit first.

1. 💻 **Typecheck and display-rule tests.** `cd rayfin-workload; npm run typecheck; npm test`. **Expected:** `tsc --noEmit` reports nothing, and all 60 tests pass (`node --test`) — `inventory.ts`'s tests were already present from T-104 and are unaffected by the new page (verified in this session).
2. 💻 **Backend offline checks.** `python -m app.manual_tests.t304_offline_checks`. **Expected:** `All checks passed.` (verified in this session — G2.1-G2.5 incl. a candidate that failed to harvest or profile, no candidates, exceptions and roles, Accept with a stale hash and wrong state, re-harvest, data gaps, and the 404/409/422/500 cases).
3. 🌐 **Pass on conformed data.** In Rayfin (Fabric portal), open a run bound to `saleslt` whose contract needs Products, on the **Inventory** tab click **Harvest**, then **Profile candidates** (each disables the tab's other actions and shows `JobProgress` while it runs). **Expected:** the **Gate G2 checklist** shows `G2.1`, `G2.2`, `G2.3` and `G2.5` **Pass** (`G2.4` **Pass** or an amber **Warning**), and the Accept note is gone. Click **Accept**. **Expected:** the summary shows state `DATA_PROFILED` and badge **Accepted**; `call("GET", "/runs/{run_A}/approvals")` ends with an `ACCEPT` record for `MetadataInventory` with your UPN, role `data_engineer` and a `gate_result_ref`.
4. 🌐 **Exception required.** In `nexora_control` → `Files/config/conformed_sources.yaml`, remove `saleslt.SalesLT_Customer` (restore it afterwards), harvest and profile a run whose contract needs Customers, reload the Inventory tab. **Expected:** `G2.5` **Fail** naming `saleslt.SalesLT_Customer`, **Accept** disabled with "Gate G2 failed: G2.5.", **Source exceptions** shows "saleslt.SalesLT_Customer is not a conformed source".
5. 🌐 **Exception approved.** As a `data_engineer`, type a rationale under **Source exceptions** and click **Approve exception**. **Expected:** `G2.5` turns **Pass**, the exception is listed with your UPN and time, `call("GET", "/runs/{id}/approvals")` has `artifact: "Exception:saleslt.SalesLT_Customer"` with the rationale as `comment`, and **Accept** becomes available. Without the `data_engineer` role the same button is `403 FORBIDDEN` and the UI says a data_engineer must approve it.
6. 🌐 **Evidence visible.** On a run scoped to include `nexora_test.dim_product_dupes`, click **Analyze relationships**, find the table under **Tables** (use the search box) and **Show columns**. **Expected:** the duplicate-count warning; **Show evidence** shows the ProductID numbers and the `GROUP BY [ProductID] HAVING COUNT(*) > 1` query. Under **Relationship evidence**, **Show SQL** on an entry into that table shows `many_to_many` with the "not unique" note.
7. 🌐 **Data gap.** On a run in `REQUIREMENT_APPROVED`, type a description under **Raise a data gap** and click it. **Expected:** state `NEEDS_INPUT`, a **Data gap** message on the Overview tab's run detail.
8. 🌐 **Search and paging.** Type a filter into **Search tables**. **Expected:** only matching tables listed, with "n of m tables"; **Previous**/**Next** page through more than 50.
9. 🌐 **Re-harvest after Accept.** Click **Re-harvest**. **Expected:** state returns to `REQUIREMENT_APPROVED`, inventory version +1 and **Draft**, profiles gone, `G2.3` fails until profiled again.
10. 🌐 **M3 exit.** Using only Rayfin: Harvest, Profile candidates, Analyze relationships, Accept. **Expected:** an accepted inventory (`status: approved`) exists for the run, reviewable end to end on the Inventory tab.

**Not run by Claude:** steps 3–10, which need a live Fabric tenant, the Rayfin workload deployed into the real Starter-Kit, and the real Lakehouse. Claude verified steps 1 and 2.

---

## M4 — Planning

### T-401 — Entity-to-source mapping and conformed-table resolver

> **Fabric re-test (2026-10-21).** Logic unchanged. `POST /runs/{id}/plan/sources` is `job:`; `GET /runs/{id}/plan/sources` is `udf:` (`CONTRACTS.md#fabric-operations`, confirmed against `app/fabric/dispatch.py`). `config/glossary.yaml` is now read from `Files/config/` of `nexora_control` in a job, the same way as T-202's `critical_terms.yaml` (fixed this pass: `discovery/glossary.py` now calls `app.config_files.from_fabric()`/`read_text()`; local development and every offline check are unaffected). Same known `ArtifactStore` gap as T-301 for a later `GET /runs/{id}/plan/sources` udf read of a just-resolved run; the job's own write is unaffected.

1. 💻 **Offline checks.** `python -m app.manual_tests.t401_offline_checks`. **Expected:** `All checks passed.` (verified in this session, after the glossary fix above — glossary loading and errors, scoring and tie-breaks, selection of Sales, Product and Customer, rationale and key evidence, rejected tables with reasons, the conformed policy and approved exceptions, KPI field mapping with hallucinated tables, columns and roles discarded, a names-and-types-only model payload, an identical file on a second call, a glossary edit making a new version, and the 404/409/500/502 cases).
2. 🌐 **Correct selection, as a job.** On a run bound to `saleslt`, `DATA_PROFILED`, whose contract needs Sales, Product and Customer and a Net Revenue KPI (run A): reserve and run `POST /runs/{id}/plan/sources` (`body = {}`) through `nexora_stage_runner`. **Expected:** the job result's `entities` has Sales selected as `saleslt.SalesLT_SalesOrderDetail`, Product as `saleslt.SalesLT_Product` and Customer as `saleslt.SalesLT_Customer`, each with a `score`, `conformed: true`, `exception_id: null` and a `rationale` (for example "Glossary match 'product'; conformed; key ProductID unique"). `kpi_fields` for Net Revenue maps the line amount to `SalesLT_SalesOrderDetail.LineTotal` (`role: measure`). `call("GET", "/runs/{run_A}/artifacts")` lists `SourceResolution` v1 as `draft`.
3. 🌐 **Rejections explained.** On run B (scope `saleslt` and `nexora_test`, profiled including `nexora_test.dim_product_dupes`): run the same job. **Expected:** under Product, `rejected` holds `nexora_test.dim_product_dupes` with a lower score, `requires_exception: true` and a reason mentioning conformed status or uniqueness.
4. 🌐 **Conformed policy.** In `nexora_control` → `Files/config/conformed_sources.yaml`, remove `saleslt.SalesLT_Product` (restore it afterwards) and run the job again on run A. **Expected:** Product has `selected: null` with a reason naming the need for an exception, and `SalesLT_Product` in `rejected` with `requires_exception: true`. Approve an exception first (`call("POST", "/runs/{id}/exceptions", {}, {"table": "saleslt.SalesLT_Product", "rationale": "pilot"})` while `REQUIREMENT_APPROVED`), then run it again: Product is selected with `conformed: false` and the exception's id.
5. 🌐 **Glossary without code.** In `Files/config/glossary.yaml`, remove `item` from the `product` list, create a run whose contract entity is `Items` and run the job. **Expected:** `Items` has `selected: null`. Add `item` back and run again. **Expected:** `Items` maps to `saleslt.SalesLT_Product` (`SourceResolution` version 2), with no code change. Break the file (a term whose value is not a list) and run it. **Expected:** the job result is `500 CONFIG_ERROR` naming `glossary.yaml` and the line.
6. 🌐 **Deterministic.** Run the job twice on an unchanged run. **Expected:** the version stays 1 and `SourceResolution.json` (`python -m app.cli runs pull <run>`) is byte for byte the same.

**Not run by Claude:** steps 2–6, which need a live Fabric tenant, the real model and the real Lakehouse. Claude verified step 1.

---

### T-402 — KPI feasibility and Data Gap

> **Fabric re-test (2026-10-21).** Logic unchanged. `POST /runs/{id}/plan/feasibility` is `job:` (`CONTRACTS.md#fabric-operations`, confirmed against `app/fabric/dispatch.py`). It reads only control files (`SourceResolution.json`, the contract) and no new config file, so nothing else changed. Same known `ArtifactStore` gap as T-301 for a later udf read of the run's files.

1. 💻 **Offline checks.** `python -m app.manual_tests.t402_offline_checks`. **Expected:** `All checks passed.` (verified in this session — feasible and data-gap KPIs, the deterministic override, a model claiming a column the inventory lacks, one missing exclusion making a gap, time logic with and without a date column, needs the source resolution couldn't map, all KPIs gaps, NEEDS_INPUT and the data gap list, no DAX in the run folder, the one retry and 502 on unusable output, and the 404/409 cases).
2. 🌐 **Unsupported KPI stopped, as a job.** On run B (same contract as T-401's run A plus `Customer Retention Rate by quarter`, which `saleslt` can't support), after `/plan/sources`: reserve and run `POST /runs/{id}/plan/feasibility` (`body = {}`). **Expected:** `Customer Retention Rate` is `data_gap` with `missing` entries (each with a reason) and `options: ["drop_kpi", "provide_source", "data_engineering_task"]`. The run's `state` is `NEEDS_INPUT` with `resume_state: DATA_PROFILED`, and `data_gaps` has an entry for the KPI. `python -m app.cli runs pull <run_B>` then search the folder for `dax`, `evaluate` and `proxy`. **Expected:** no matches.
3. 🌐 **Supported KPI passes.** Run the same job on run A (Net Revenue only). **Expected:** `Net Revenue` is `feasible`, `missing: []`, and every `needed` entry has `mapped` set to a column such as `saleslt.SalesLT_SalesOrderDetail.LineTotal`. The state stays `DATA_PROFILED`; `call("GET", "/runs/{run_A}/artifacts")` lists `Feasibility`.
4. 🌐 **Deterministic override.** Pull run A, remove the `LineTotal` field entry from `kpi_fields` in `SourceResolution.json`, upload it back (or edit it in `Files/runs/<run_A>/` of `nexora_control` directly), then run the feasibility job again. **Expected:** Net Revenue is `data_gap`, `missing` names the line amount, `Feasibility.json` is version 2 and the state is `NEEDS_INPUT`. Restore it with `python -m app.cli runs transition <run_A> DATA_PROFILED` then re-run `/plan/sources`.
5. 🌐 **Repeatable.** Run the job twice on a feasible, unchanged run. **Expected:** the version stays 1 and `Feasibility.json` is byte for byte the same.

**Not run by Claude:** steps 2–5, which need a live Fabric tenant, the real model and the real Lakehouse. Claude verified step 1.

---

### T-403 — Bridge-table detection and proposal

> **Fabric re-test (2026-10-21).** Logic unchanged. `POST /runs/{id}/plan/bridges` is `job:`; `POST /runs/{id}/approvals` with `BridgeProposal:<id>` is `udf:` (`_approve_host`, confirmed against `app/fabric/dispatch.py`). `config/derivation_rules.yaml` is now read from `Files/config/` the same way as T-401's `glossary.yaml` (fixed this pass: `discovery/derivation_rules.py` now calls `app.config_files.from_fabric()`/`read_text()`; local development and every offline check are unaffected). Same known `ArtifactStore` gap as T-301 for a later udf read of the run's files.

1. 💻 **Offline checks.** `python -m app.manual_tests.t403_offline_checks`. **Expected:** `All checks passed.` (verified in this session, after the derivation-rules fix above — rule file errors, an existing mapping table with duplicates and coverage, several mapping tables, a derivation rule, a deduplication rule, `needs_rules` and `NEEDS_INPUT`, an isolated entity, a normal star with no proposals, a many-to-many nobody needs, approvals by both roles, the 403/404/409/422 cases, and 400/404/500 preconditions).
2. 🌐 **Existing mapping proposed, as a job.** On run A (`nexora_test` fixtures present, contract entities Sales, Product and Promotion, scope `saleslt` and `nexora_test`, harvested, profiled, relationships analyzed, inventory accepted, `/plan/sources` run): reserve and run `POST /runs/{id}/plan/bridges` (`body = {}`). **Expected:** one proposal `id: "B1"`, `entities: ["Product", "Promotion"]`, `situation: "existing_table"`, `table: "nexora_test.bridge_product_promotion"`, `status: "proposed"`, `evidence.duplicate_pairs: 0`, `coverage_a_pct` (Product, well under 100) and `coverage_b_pct: 100.0` (Promotion); `evidence.queries` shows the aggregate SQL. The state is still `DATA_PROFILED`.
3. 🌐 **No guess.** On run B (entities Product and Campaign, same scope, same preparation): run the same job. **Expected:** one proposal with `situation: "needs_rules"`, `table: null`, `derivation_rule: null`. The run state is `NEEDS_INPUT` and `call("GET", "/runs/{run_B}")` shows a data gap asking for mapping rules for Product and Campaign.
4. 🌐 **Can't approve without rules.** `call("POST", "/runs/{run_B}/approvals", {}, {"artifact": "BridgeProposal:B1", "action": "ACCEPT", "expected_hash": "<BridgeProposals hash from call(\"GET\", \"/runs/{run_B}/artifacts\")>"})`. **Expected:** `409 MAPPING_RULES_REQUIRED`; the proposal stays `proposed`.
5. 🌐 **Approve a real bridge.** The same call on run A with `BridgeProposal:B1` (a wrong hash first gives `409 STALE_VERSION`). **Expected:** `200`, the proposal's status is `approved`, `BridgeProposals` is version 2 with status `approved`, and `call("GET", "/runs/{run_A}/approvals")` ends with an `ACCEPT` record for `BridgeProposal:B1` with your UPN and role `bi_engineer`. A second approval is `409 ALREADY_APPROVED`.
6. 🌐 **Derivation rule.** In `Files/config/derivation_rules.yaml` add `rules: - id: DR1, entities: [Product, Campaign], description: Campaign applies to the products of its category, approved_by: <you>` (restore it afterwards), then run the bridges job on a fresh run like B. **Expected:** `situation: "derivation_rule"`, `derivation_rule: "DR1"`, no table, state stays `DATA_PROFILED`.
7. 🌐 **No false alarm.** On a run with entities Sales, Product and Customer (scope `saleslt`), run the bridges job. **Expected:** `[]` and the state unchanged.

**Not run by Claude:** steps 2–7, which need a live Fabric tenant and the real Lakehouse with the `nexora_test` fixtures. Claude verified step 1.

---

### T-404 — ExecutionPlan and ModelSpec generation

> **Fabric re-test (2026-10-21).** Logic unchanged. `POST /runs/{id}/plan` and `GET /runs/{id}/plan` are `udf:` operations (`CONTRACTS.md#fabric-operations`, confirmed against `app/fabric/dispatch.py`): generation is deterministic and touches only control files. `config/conformed_sources.yaml` is now read from `Files/config/` the same way as T-401's `glossary.yaml` (fixed this pass: `workflow/conformed.py` now calls `app.config_files.from_fabric()`/`read_text()`). `python -m app.cli plan validate runs/<id>` resolves a run id through the artifact store (T-101's `cli.py:_resolved_run_folder`) and is otherwise unaffected. Same known `ArtifactStore` gap as T-301 for the `udf:` calls themselves, until the retrofit lands.

1. 💻 **Offline checks.** `python -m app.manual_tests.t404_offline_checks`. **Expected:** `All checks passed.` (verified in this session, after the conformed-sources fix above — the plan for a normal star, hierarchy, hidden keys, evidence refs, determinism, the CLI as a command, every validation check failing with its path, RLS from the contract only, the date-table action, a bridge in the plan, only feasible KPIs, `NO_FEASIBLE_KPIS`, and the 404/409/500 cases).
2. 🌐 **Plan generated.** On run A (Net Revenue plus a monthly KPI with time logic "by month", entities Sales, Product and Customer, scope `saleslt` and `nexora_test`, harvested, profiled, relationships analyzed including the order-header and date joins, inventory accepted, `/plan/sources` and `/plan/feasibility` run): `call("POST", "/runs/{id}/plan")`. **Expected:** `200` with `execution_plan`, `model_spec` and `validation`. `ModelSpec` has a `Sales` table with `role: fact`, `grain: order line` and source `SalesLT_SalesOrderDetail`; its relationships go to Product and to the order header, and from the header to Customer and Date, each with an `evidence_ref` of `high` or `medium` confidence. `storage_mode` is `DirectLake`, `Date` has a `Calendar` hierarchy, and `Net Revenue` is a measure with `dax: null`. `validation.passed` is `true`. The state is still `DATA_PROFILED`.
3. 🌐 **Bad reference caught.** Pull run A, change the `to:` column of the `Sales_Product` relationship in `ModelSpec.yaml` to `NoSuchColumn`, upload it back, then run `python -m app.cli plan validate runs/<run_A>` (against the Fabric stores). **Expected:** `INVALID`, a line `PLAN.1: relationships[Sales_Product].to.column: Product.NoSuchColumn`, exit code 1. `call("GET", "/runs/{run_A}/plan")` shows the same failure. Undo the edit, or call `POST /runs/{run_A}/plan` to regenerate: `VALID`, exit 0.
4. 🌐 **No inferred security.** Check `roles` in run A's `ModelSpec.yaml` (no contract security requirements). **Expected:** `roles: []`. On a run whose contract has a security requirement, the plan has one `RLS<n>` role with that description.
5. 🌐 **Missing date handled.** On run B (scope `saleslt` only, no date table): `call("POST", "/runs/{run_B}/plan")`. **Expected:** `execution_plan.data_engineering_actions` has one blocking, unowned `create_date_table` action; `ModelSpec` has no `date`-role table and no calculated table; `validation.passed` is `true`.

**Not run by Claude:** steps 2–5, which need a live Fabric tenant, the real model and the real Lakehouse. Claude verified step 1.

---

### T-405 — Gate G3 and plan review UI

> **Fabric rework (2026-10-21).** The backend (Gate G3, plan edits, the `ExecutionPlan` approval) is T-404's, unchanged, confirmed wired through the dispatcher: `POST /runs/{id}/gates/G3/check` and `PATCH /runs/{id}/plan` are `udf:`; `POST /runs/{id}/approvals` on `ExecutionPlan` is `udf:` too, since only `REGENERATE` is ever `job:` and that action isn't valid for this artifact (`_approve_host`). The UI is new: a **Plan** tab (`rayfin-workload/src/pages/PlanPage.tsx`) added to the **Nexora Run** item editor (`src/items/NexoraRunItem/NexoraRunItemEditor.tsx`), replacing `rayfin-ui`'s `/runs/[id]/plan`. `src/lib/plan.ts` and its tests were already ported in T-104/this task and are unchanged here; `src/lib/api.ts` gained `getSourceResolution`, `resolveSources`, `checkFeasibility` (jobs, via `runJob`) and `checkG3`, `getPlan`, `generatePlan`, `editPlan` (udf, via `call`). There is no dev server for `rayfin-workload` yet (see its README), so the 🌐 steps need this project dropped into the real Extensibility Toolkit Starter-Kit first.

1. 💻 **Typecheck and display-rule tests.** `cd rayfin-workload; npm run typecheck; npm test`. **Expected:** `tsc --noEmit` reports nothing, and all 60 tests pass (`node --test`) — `plan.ts`'s tests were already present and are unaffected by the new page (verified in this session).
2. 💻 **Backend offline checks.** `python -m app.manual_tests.t405_offline_checks`. **Expected:** `All checks passed.` (verified in this session — G3.1-G3.7 each passing and failing, the four edit operations, atomic and invalid edits, stale hashes, versions kept, EDIT records, Accept with G3 and roles, `GATE_FAILED`, reject and reopen, approving again, and the 404/409/422/500 cases).
3. 🌐 **Rationale visible.** In Rayfin (Fabric portal), open run A's (T-404) **Plan** tab. **Expected:** every table under **Tables** shows its role, source and a "Why: …" line; **Rejected alternatives** lists the tables not chosen with their reason; **Relationships** shows each relationship's evidence and **Show SQL**; **Measures**, **Storage mode**, **Row-level security** and **Data-engineering actions** are listed too.
4. 🌐 **Edit re-validates.** Click **Remove** on the relationship into the date table. **Expected:** the tab reloads as the next plan version, **Gate G3 checklist** shows `G3.6` **Fail** naming the measure that needs the date, and **Accept** is disabled with "Gate G3 failed: G3.6." **History** has an `EDIT` entry. Via `call("PATCH", "/runs/{run_A}/plan", {}, {"expected_hash": "<ExecutionPlan hash>", "ops": [{"op": "explode"}]})`: `422` listing the four valid ops.
5. 🌐 **Approve.** Click **Regenerate plan** (or fix the edit) so G3 passes again, then **Accept** as a `bi_engineer`. **Expected:** state `PLAN_APPROVED`, badge **Approved**; `call("GET", "/runs/{run_A}/approvals")` ends with an `ACCEPT` record for `ExecutionPlan` whose `artifact_hash` matches. **Reject** (with a comment) on the approved plan reopens it to `DATA_PROFILED`.
6. 🌐 **Role enforced.** As a user without `bi_engineer` in `Files/config/roles.yaml`, reload the tab. **Expected:** **Accept** says only a `bi_engineer` can accept or reject. `call("POST", "/runs/{run_A}/approvals", {}, {"artifact": "ExecutionPlan", "action": "ACCEPT", "expected_hash": "<hash>"})` is `403 FORBIDDEN`.
7. 🌐 **Blocking action.** On run B (T-404, no date table), generate the plan. **Expected:** **Data-engineering actions** shows the blocking, unowned `create_date_table` action, `G3.5` **Fail**, **Accept** unavailable.
8. 🌐 **M4 exit.** Using Rayfin only: Resolve sources, Check feasibility, Generate plan, review, Accept. **Expected:** an approved execution and model plan exists (`status: approved`, run state `PLAN_APPROVED`) and is reviewable end to end on the Plan tab.

**Not run by Claude:** steps 3–8, which need a live Fabric tenant, the Rayfin workload deployed into the real Starter-Kit, and the real Lakehouse. Claude verified steps 1 and 2.

---

## M5 — Semantic model

### T-501 — Sandbox deployment client

> **Fabric re-test (2026-10-21).** Logic unchanged; `FabricItemsClient` has no operation of its own (a library jobs call, [CONTRACTS.md#fabric-operations](tasks/CONTRACTS.md#fabric-operations)). Confirmed in this pass: the token already comes from `get_token(FABRIC_SCOPE)` (T-002), never OBO. The CLI steps below are unaffected by hosting and still the most direct way to exercise the client; `SANDBOX_WORKSPACE_ID` comes from the Variable library in Fabric, `.env` locally.

**Before you start:** put the sandbox workspace's ID in `SANDBOX_WORKSPACE_ID` in `.env` (a workspace you may write to, never a production one). Create this folder, indenting with tabs:

```
tmp\Nexora_Smoke\definition.pbism                  {"version": "4.0", "settings": {}}
tmp\Nexora_Smoke\definition\database.tmdl          database Nexora_Smoke
                                                   	compatibilityLevel: 1604
tmp\Nexora_Smoke\definition\model.tmdl             model Model
                                                   	culture: en-US
                                                   ref table Product
tmp\Nexora_Smoke\definition\tables\Product.tmdl    table Product
                                                   	column ProductID
                                                   		dataType: int64
                                                   		sourceColumn: ProductID
                                                   		summarizeBy: none
                                                   	measure 'Product Count' = COUNTROWS('Product')
                                                   	partition Product = m
                                                   		mode: import
                                                   		source = let Source = #table({"ProductID"}, {{1}, {2}}) in Source
```

The table holds two literal rows, so no Lakehouse connection is needed. If Fabric rejects the files, adjust them to Microsoft's current TMDL documentation. The error you see is also what step 4 tests.

1. 🌐 **Create.** `python -m app.cli deploy model tmp\Nexora_Smoke --name Nexora_Smoke`. **Expected:** `item_id: <guid>` and `operation: created`, exit code 0. The model `Nexora_Smoke` appears in the sandbox workspace and opens in the web model view.
2. 🌐 **Update, not duplicate.** Rename the measure in `Product.tmdl` (for example to `Products`) and run the same command again. **Expected:** the same `item_id` and `operation: updated`. The workspace still has exactly one `Nexora_Smoke`, showing the new measure name.
3. 🌐 **Guard.** `python -m app.cli deploy model tmp\Nexora_Smoke --name Nexora_Smoke --workspace-id 99999999-9999-4999-8999-999999999999; echo $LASTEXITCODE`. **Expected:** `TARGET_NOT_ALLOWED: Deployment is only allowed to the configured sandbox workspace`, exit code 1, and nothing created anywhere (no Fabric call is made).
4. 🌐 **Failure surfaced.** Put a syntax error in `Product.tmdl` (for example delete the `=` of the measure) and deploy. **Expected:** `UPSTREAM_ERROR: ...` with Fabric's message, `Fabric request ID: <id>` and Fabric's error JSON, exit code 1. Fix the file afterwards.
5. 💻 **Offline checks.** `python -m app.manual_tests.t501_offline_checks`. **Expected:** `All checks passed.` (verified in this session — the definition parts, create, update without a duplicate, polling with `Retry-After`, a failed operation, a rejected definition, `429` retries, a name used by another type, the timeout, the target guard, the setting fallback and the CLI).
6. 💻 **Clean up.** Delete `Nexora_Smoke` from the sandbox workspace and the `tmp\Nexora_Smoke` folder.

**Not run by Claude:** the live steps (1–4), which need your sign-in and the sandbox workspace. Claude ran step 5.

---

### T-502 — TMDL generator

> **Fabric re-test (2026-10-21).** Logic unchanged; confirmed wired through the dispatcher: `POST /runs/{id}/model/generate` is `udf:` ([CONTRACTS.md#fabric-operations](tasks/CONTRACTS.md#fabric-operations)). Fixed in this pass: `app/cli.py`'s `_resolved_run_folder` (used by `model generate runs/<id>` when `ARTIFACT_STORE=onelake` with no local mount) now pulls a run's files into a folder whose basename is the run id, and `model generate` now reports an unknown run as `File not found: ...` (exit 2) instead of trying to generate from an empty folder. The ArtifactStore retrofit the task's Fabric note describes (writing through `copy_tree` instead of a local `Path`) is **not done**: see the "Known gap" note in this file's "Target hosting" section and the note under this task's own CLAUDE.md section.

**Before you start:** you need a run in `PLAN_APPROVED`. On Fabric, use Rayfin's Plan tab (T-405) to get there, or the `nexora_api` test pane with `call("POST", "/runs/{id}/jobs", {}, {"operation": "POST /runs/{id}/plan", "body": {}})` style calls per [CONTRACTS.md#fabric-operations](tasks/CONTRACTS.md#fabric-operations). Its Lakehouse must have a SQL endpoint. `SANDBOX_WORKSPACE_ID` must be set for step 3 (Variable library in Fabric, `.env` locally).

1. 🌐 **Generated.** `call("POST", "/runs/{run_A}/model/generate")`. **Expected:** HTTP 200 with `version: 1`, `files` and `run.state: MODEL_GENERATING`. `python -m app.cli runs pull {run_A}` then shows `model\v1\` with `definition.pbism` and `definition\` with `database.tmdl`, `model.tmdl`, `relationships.tmdl`, `expressions.tmdl` and one file under `tables\` per ModelSpec table. `call("GET", "/runs/{run_A}/artifacts")` lists `TMDLModel` (version 1, draft). A second call is `409 WRONG_STATE`.
2. 🌐 **Deterministic.** `python -m app.cli model generate runs\{run_A}` (pulls the run first when it isn't already a local folder), then `git diff --no-index <pulled>\model\v1 <pulled>\model\v2`. **Expected:** the command prints `v2: <n> files in ...` and `git diff` prints nothing.
3. 🌐 **Deployable.** `python -m app.cli deploy model <pulled>\model\v1 --name Nexora_<run>` (use the first 8 characters of the run id). **Expected:** `operation: created`. In the web model view of the sandbox workspace the tables, the relationships and the Calendar hierarchy of the date table are visible. Measures show only after T-503 (their `dax` is still null).
4. 🌐 **No LLM.** The generator never imports `app.llm.client`, so it isn't affected by the model endpoint at all; confirm by running `model generate` again with no network path to Azure OpenAI reachable. **Expected:** it still succeeds.
5. 💻 **Offline checks.** `python -m app.manual_tests.t502_offline_checks`. **Expected:** `All checks passed.` (verified in this session, including the CLI's unknown-folder and deterministic-regeneration checks — the file layout, quoting, tabs and LF, Direct Lake and Import partitions, the hierarchy, relationships, null DAX left out, byte-identical output in any object order, refusals that write nothing, the endpoint with its 404/409/400/422 cases, `MODEL_FAILED` for repair, and the CLI).
6. 💻 **Clean up.** Delete the model from the sandbox workspace and the pulled local folder.

**Not run by Claude:** the live steps (1–4), which need a live Fabric tenant, Rayfin or the `nexora_api` test pane, and the sandbox workspace. Claude ran step 5 and fixed the CLI bug it (and step 2) depend on.

---

### T-503 — DAX measure generation

> **Fabric re-test (2026-10-21).** Logic unchanged; confirmed wired through the dispatcher: `POST /runs/{id}/model/measures` is `job:` ([CONTRACTS.md#fabric-operations](tasks/CONTRACTS.md#fabric-operations)), called by `nexora_api`'s `call("POST", "/runs/{id}/jobs", {}, {"operation": "POST /runs/{id}/model/measures", "body": {}})` + `nexora_stage_runner`. No config or token change was needed.

**Before you start:** run A of T-502, in state `MODEL_GENERATING` with `model\v1` generated (Net Revenue with the exclusion of cancelled orders, a monthly KPI with time logic, scope with `nexora_test.dim_date`). The Environment `nexora_env` must reach the real model. Make sure the Net Revenue KPI's exclusions state the status value to exclude (for example "Cancelled orders have Status = 6"), because the model never sees data and the pre-check rejects a number the definition doesn't state.

1. 🌐 **Measures added.** Reserve and run the job for `POST /runs/{run_A}/model/measures` (Rayfin's Model tab "Add measures", or the test-pane/job-reservation steps of [CONTRACTS.md#fabric-operations](tasks/CONTRACTS.md#fabric-operations)). **Expected:** `version: 2`, and Net Revenue `accepted` with `dax` that uses only ModelSpec objects and applies the exclusion of cancelled orders. `python -m app.cli runs pull {run_A}` shows `ModelSpec.yaml` at version 2 with that `dax` and a `description` starting `Approved: <the formula intent>`, and `model\v2\definition\tables\<fact table>.tmdl` has the measure; `model\v1` is unchanged. A second call returns the same measures and `version: 2` without calling the model.
2. 🌐 **Deployed and computes.** `python -m app.cli deploy model <pulled>\model\v2 --name Nexora_<run>`. In the web model view, run `EVALUATE ROW("NR", [Net Revenue])`. **Expected:** a number.
3. 🌐 **Matches SQL.** In the Lakehouse SQL endpoint run the equivalent query, for example `SELECT SUM(d.LineTotal) FROM saleslt.SalesLT_SalesOrderDetail d JOIN saleslt.SalesLT_SalesOrderHeader h ON h.SalesOrderID = d.SalesOrderID WHERE h.Status <> 6`. **Expected:** the same value as step 2. (T-504 automates this.)
4. 💻 **Bad reference rejected.** Covered by the offline checks: a proposal that uses `Sales[Nope]` gets one retry carrying "unknown column 'Nope' ...", and if the retry fails too the measure is `rejected` with a reason naming `Sales[Nope]`, while the other measures are still processed.
5. 💻 **Offline checks.** `python -m app.manual_tests.t503_offline_checks`. **Expected:** `All checks passed.` (verified in this session — reading DAX references, every rejection rule, the 2-attempt graph path, `date table required`, the endpoint with accepted and rejected measures, the replay without a model call, the TMDL and ModelSpec versions, and the 404/409 cases).
6. 💻 **Clean up.** Delete the model from the sandbox workspace.

**Not run by Claude:** the live steps (1–3), which need a live Fabric tenant, the real model and the sandbox workspace. Claude ran steps 4 and 5 (offline).

---

### T-504 — Model validators and Gate G4

> **Fabric re-test (2026-10-21).** Logic unchanged; confirmed wired through the dispatcher: `POST /runs/{id}/model/validate` is `job:` ([CONTRACTS.md#fabric-operations](tasks/CONTRACTS.md#fabric-operations)). Fixed in this pass: `config/validation.yaml` and `config/relationship_thresholds.yaml` now read from `Files/config/` of `nexora_control` in a Fabric job, the same pattern T-202 used for `critical_terms.yaml` (local development is unaffected). Confirmed: deployment, refresh and `executeQueries` already use `get_token(FABRIC_SCOPE)`/`get_token(POWERBI_SCOPE)` (T-002), never OBO, so the old build's Entra app registration and its `Dataset.ReadWrite.All` consent no longer apply — but whether a job's `pbi`-audience token is sufficient on its own is **not yet confirmed against a live tenant** (see `fabric-hosting.md`'s platform-limits table, row added in this pass); step 1 below is where that gets checked.

**Before you start:** the T-503 setup (run A in `MODEL_GENERATING` with the measures added and `model\v2` generated), plus `SANDBOX_WORKSPACE_ID` set. Validation has no role gate, so `config\roles.yaml` needs no change.

1. 🌐 **Passing model.** Run the job for `POST /runs/{run_A}/model/validate`. **Expected:** `report.passed: true`, the run still `MODEL_GENERATING`. If the job fails with a Power BI permission or consent error instead, record that in `fabric-hosting.md`'s platform-limits table — that is the unconfirmed item this step exists to check. `ValidationReport.json` (pulled with `runs pull`) has one run with `G4.REF`, one `G4.REL` per relationship, `G4.DAX` and `G4.RECON` per measure, and `G4.RLS` ("no roles defined" for a contract without security). The `G4.RECON` evidence for Net Revenue shows `dax_query`, `dax_value`, `sql_query` and `sql_value` side by side. A second call appends attempt 2 and reuses the saved SQL.
2. 🌐 **Broken measure fails.** In the pulled `model\v2\definition\tables\<fact table>.tmdl`, change `Sales[LineTotal]` in the Net Revenue measure to `Sales[LineTotalX]`, push it back (`python -m app.cli runs pull` is read-only, so edit the file where the job can see it, or test this against `RUN_STORE=file`/`ARTIFACT_STORE=local` locally instead), and validate again. **Expected:** `G4.REF` fails with "column 'LineTotalX' not found in table 'Sales'", `G4.DAX` fails with the engine's error (or `DEPLOY_FAILED`), a `FILES_CHANGED` warning is present, and the run is `MODEL_FAILED`.
3. 🌐 **Invalid relationship.** Use a run whose plan has `nexora_test.dim_product_dupes` as the product dimension, with the Sales-to-Product relationship under an override (`PATCH /runs/{id}/plan`, udf). Accept the plan, generate the model and measures, then validate. **Expected:** `G4.REL` fails for that relationship with "N duplicate key value(s) on the 'one' side" and the queries, and the run is `MODEL_FAILED`.
4. 💻 **Report blocked.** `python -m app.cli runs transition <run_A> REPORT_GENERATING` while the run is `MODEL_FAILED`. **Expected:** `ILLEGAL_TRANSITION`; the offline checks assert the state machine the same way.
5. 🌐 **Tolerance.** On a new run, before approving the contract, set a wrong example with `call("PUT", "/runs/{id}/contract/kpis/Net Revenue", {}, {"expected_hash": "<contract hash>", "expected_examples": [{"description": "Total net revenue", "value": 1}]})`; take the run through plan, model, measures and validate. **Expected:** `G4.RECON` for Net Revenue fails, showing both values and the difference.
6. 💻 **Offline checks.** `python -m app.manual_tests.t504_offline_checks`. **Expected:** `All checks passed.` (verified in this session after the config-file fix — the tolerance, the reference-SQL guard, the Power BI client, every G4 check passing and failing, skipped checks, the report and artifact, the move to `MODEL_FAILED`, replay of the saved SQL, and the 404/409/500 cases).
7. 💻 **Clean up.** Delete the `Nexora_<run>` models from the sandbox workspace, restore any edited files.

**Not run by Claude:** the live steps (1-3, 5), which need a live Fabric tenant, the real model, the sandbox workspace and the Lakehouse. Claude ran steps 4 and 6 (offline).

---

### T-505 — Validation and Repair service

> **Fabric re-test (2026-10-21).** Logic unchanged; confirmed wired through the dispatcher: `POST /runs/{id}/model/repair` is `job:` ([CONTRACTS.md#fabric-operations](tasks/CONTRACTS.md#fabric-operations)). Fixed in this pass: `config/repair_policy.yaml` now reads from `Files/config/` of `nexora_control` in a Fabric job, the same pattern as T-504's config files; local development and the offline checks are unaffected.

**Before you start:** the T-504 setup. Each step below uses a fresh run in `MODEL_GENERATING` with the measures added and `model\v2` generated, validated once.

1. 🌐 **Targeted repair.** Introduce the same `Sales[LineTotalX]` typo as T-504 step 2 and validate (the run becomes `MODEL_FAILED`). Then run the job for `POST /runs/{id}/model/repair`. **Expected:** `outcome: passed`, one attempt, `final_state: MODEL_GENERATING`. `git diff --no-index` on the pulled `model\v2` and `model\v3` shows only the fact table's file changed; `ValidationReport.json` shows the failed run, then a run with `repair_attempt: 1` that passed.
2. 🌐 **Escalation.** Rename a column a measure uses (in the Lakehouse, or in a Direct Lake table's deployed TMDL), then validate. **Expected:** a failing `G4.DAX` check naming the column. `POST /runs/{id}/model/repair` then returns `outcome: user_input_only`, no attempts, `final_state: NEEDS_INPUT`, with a `message` naming the column. `call("GET", "/runs/{id}")` shows `resume_state: MODEL_FAILED`. Restore the column afterwards.
3. 🌐 **Retry limit.** Set `reconciliation.relative_tolerance` and `absolute_tolerance` to `0` in `config\validation.yaml` (and republish it to `Files/config/` via `deploy_home`, or edit it locally for a local-store run; restore afterwards) so a rounding difference fails `G4.RECON`, then validate and repair. **Expected:** at most 3 attempts (or an earlier `no_progress` stop), then `NEEDS_INPUT` with a message listing what remains.
4. 🌐 **Audit.** Open the pulled `ValidationReport.json` after step 1 or 3. **Expected:** one run per validation and per repair attempt, each with `attempt`, `repair_attempt`, `artifact_version` and a `fingerprint` per check.
5. 🌐 **Graph boundary.** After step 3, `call("GET", "/runs/{id}")`. **Expected:** `history` has one entry for the repair call (`MODEL_FAILED -> NEEDS_INPUT`), not one per attempt.
6. 💻 **Offline checks.** `python -m app.manual_tests.t505_offline_checks`. **Expected:** `All checks passed.` (verified in this session after the config-file fix — fingerprints, the policy, every outcome, the scope guard dropping an attempt, the deterministic fix, the artifacts and the single transition, and the 404/409/422 cases).
7. 💻 **Clean up.** Restore `config\validation.yaml`, delete the models from the sandbox workspace.

**Not run by Claude:** the live steps (1-5), which need a live Fabric tenant, the real model, the sandbox workspace and the Lakehouse. Claude ran step 6.

---

### T-506 — Bridge-table generation

> **Fabric re-test (2026-10-21).** Logic unchanged; confirmed wired through the dispatcher: `POST /runs/{id}/model/bridges/{bridge_id}/generate` is `job:` ([CONTRACTS.md#fabric-operations](tasks/CONTRACTS.md#fabric-operations)). Confirmed: `items_client()`, `sql_connector_factory()`, `bridge_llm()`/`recon_llm()` and `lakehouse_connector_factory()` all default to T-002's token provider, so the notebook deployment, run and harvest-back-in stay as the job's own user; the notebook stays sandbox-only regardless of hosting. No config file is read here.

**Before you start:** run A of T-505 with the T-403 data: a run on scope `saleslt` and `nexora_test` with entities including Product and Promotion, the relationships analyzed, bridge proposal `B1` (Product x Promotion, `existing_table`) approved, the plan accepted, the model generated with measures, and validated once (that is the baseline for the totals). The run's Lakehouse must be in the sandbox workspace.

1. 🌐 **Bridge generated.** Run the job for `POST /runs/{run_A}/model/bridges/B1/generate`. **Expected:** a `version` one higher than the validated one, `validation.passed: true`, and the checks `G4.BRIDGE.UNIQUE`, two `G4.BRIDGE.ORPHANS` and `G4.BRIDGE.TOTALS` all `pass`. The pulled `ModelSpec.yaml` has `bridge_product_promotion` with `role: bridge` and two `many_to_one` relationships from it. In the web model view of the deployed model, the bridge and its two relationships are visible.
2. 🌐 **No double counting.** On the previous model version and on the new one, run `EVALUATE ROW("All", [Net Revenue])` in the web model view, and compare both with the SQL total. **Expected:** all three values match, and the `G4.BRIDGE.TOTALS` evidence shows `total_before` and `total_with_bridge` equal.
3. 🌐 **Per-promotion values.** Run `EVALUATE SUMMARIZECOLUMNS('Promotion'[PromotionName], "NR", [Net Revenue])` and compare each row with a hand-written SQL join through the bridge. **Expected:** the values match.
4. 🌐 **Duplicates fail.** Insert a duplicate pair into `nexora_test.bridge_product_promotion` with a Spark notebook cell, then run `POST /runs/{run_A}/model/validate` as a job. **Expected:** `G4.BRIDGE.UNIQUE` fails showing the pair and its count, and the run is `MODEL_FAILED`. Remove the duplicate row afterwards by rerunning `fixtures\create_test_tables.ipynb`.
5. 🌐 **Notebook path (optional).** Add a rule to `config\derivation_rules.yaml` (and republish it) for entities Product and Region, run T-403 for a run with an unmapped Region entity, approve the proposal, then generate it. **Expected:** a notebook is deployed to and ran in the sandbox workspace, `nexora_generated.bridge_b1_product_region` exists, and the model references it.
6. 💻 **Offline checks.** `python -m app.manual_tests.t506_offline_checks`. **Expected:** `All checks passed.` (verified in this session — the notebook and the items client for notebooks and jobs, the model changes and filter directions, every G4.BRIDGE check passing and failing, deduplication under a rule, a derived bridge, the SQL guard, a failing notebook, the sandbox rule, and the 404/409/422/403/502 cases).
7. 💻 **Clean up.** Delete the models and notebooks from the sandbox workspace and the `nexora_generated` tables, restore `config\derivation_rules.yaml`.

**Not run by Claude:** the live steps (1-5), which need a live Fabric tenant, the real model, the sandbox workspace and Lakehouse. Claude ran step 6.

---

### T-507 — Model review and approval UI

> **Fabric rework (2026-10-21).** The backend is T-504/T-505/T-506's, unchanged, confirmed wired through the dispatcher: `GET /runs/{id}/model/review` is `udf:`, and `POST /runs/{id}/approvals` on `SemanticModel` is `udf:` too (`_approve_host`), matching [CONTRACTS.md#fabric-operations](tasks/CONTRACTS.md#fabric-operations). The UI is new: a **Model** tab (`rayfin-workload/src/pages/ModelPage.tsx`) on the **Nexora Run** item editor, replacing `rayfin-ui`'s `/runs/[id]/model`. `src/lib/model.ts` (display rules, ported in T-104) is unchanged; `src/lib/api.ts` gained `generateModel` (udf) and `addMeasures`, `validateModel`, `repairModel`, `generateBridge` (jobs), plus `getModelReview` (udf). There is no dev server for `rayfin-workload` yet (see its README), so the 🌐 steps need this project dropped into the real Extensibility Toolkit Starter-Kit first.

**Before you start:** the T-506 setup: a run in `MODEL_GENERATING` with the measures added, validated once (passing). Put **your own UPN** under `bi_engineer` and `business_owner` in `Files/config/roles.yaml` (restore it afterwards). Open run A's **Model** tab in Rayfin (Fabric portal).

1. 🌐 **Side-by-side review.** **Expected:** the summary shows the state, **Gate G4 passed**, the version and the model hash. Each measure shows its DAX next to its approved definition ("Formula intent: Sum of line totals excluding cancelled orders" and its exclusions), then the **DAX**, **SQL** and **Expected** reconciliation values ("No business example" if none was given). Tables, relationships (with "filters both ways" or "one way"), RLS roles, the validation and the approval history are listed too.
2. 🌐 **Partial approval.** Click **Approve as BI engineer**. **Expected:** the run is still `MODEL_GENERATING`, the BI engineer shows "Approved by <you>", and the Business owner shows **Pending**. A user without a role sees that role's Approve disabled with "Only a business_owner can approve as business_owner."
3. 🌐 **Invalidation.** Change the model in any way that alters the deployed files (a repair or generating a bridge does this naturally). Reload the tab. **Expected:** the BI engineer shows **Invalidated: model changed**, the page warns that the latest validation is of another version, and approving as the Business owner gives `409 GATE_FAILED` (G4.CURRENT). Click **Validate again**: the warning goes and **Approve as BI engineer** is available again.
4. 🌐 **Complete.** Approve as BI engineer again, then as Business owner (two separate clicks if you hold both roles). **Expected:** the run becomes `MODEL_APPROVED`, the history has one record per role, and `call("GET", "/runs/{run_A}/approvals")` ends with those `SemanticModel` records on the current hash. A contract with a security requirement also needs **Security reviewer**.
5. 🌐 **Business owner rejects the definition.** On a fresh run: type a comment, tick the definition checkbox and click **Reject as Business owner**. **Expected:** the run becomes `NEEDS_INPUT` and its history says the business owner says a KPI definition is wrong; the contract file is unchanged.
6. 🌐 **M5 exit.** Using Rayfin only: Generate model, Add measures, Validate, and the two or three approvals. **Expected:** the run is `MODEL_APPROVED`, `call("GET", "/runs/{id}/artifacts")` shows `TMDLModel` and `ModelSpec` as `approved`, and the model exists in the sandbox workspace.
7. 💻 **Offline checks.** `python -m app.manual_tests.t507_offline_checks`. **Expected:** `All checks passed.` (the review data, partial and complete approval, one user with several roles, roles not required, role and hash checks, Gate G4, invalidation when the model changes, the security reviewer, Reject and the definition option, and the 404/409/422 cases).
8. 💻 **Typecheck and display-rule tests.** `cd rayfin-workload; npm run typecheck; npm test`. **Expected:** `tsc --noEmit` reports nothing, and all 60 tests pass (`node --test`) — verified in this session.
9. 💻 **Clean up.** Restore `Files/config/roles.yaml`, delete the models from the sandbox workspace.

**Not run by Claude:** the live and browser steps (1-6), which need a live Fabric tenant and the Rayfin workload deployed into the real Starter-Kit. Claude ran steps 7 and 8.

---

## M6 — Report

### T-601 — Report planner

> **Fabric re-test (2026-10-21).** Logic unchanged; confirmed wired through the dispatcher: `POST /runs/{id}/report/plan` is `job:`, `GET /runs/{id}/report/plan` is `udf:` ([CONTRACTS.md#fabric-operations](tasks/CONTRACTS.md#fabric-operations)). No config file is read here, so nothing needed the `Files/config/` retrofit T-202/T-401/T-403/T-404 used.

**Before you start:** a run in `MODEL_APPROVED` (the M5 exit), with its model measures carrying DAX (called "run A"). `nexora_api` and `nexora_stage_runner` deployed (T-005/T-102).

1. 🌐 **Questions covered.** Reserve and run the job for `POST /runs/{run_A}/report/plan` (from Rayfin's **Report** tab, "Generate plan", or by hand: `call("POST", "/runs/{run_A}/jobs", {}, {"operation": "POST /runs/{id}/report/plan", "body": {}})` then run `nexora_stage_runner` with that `job_id`/`run_id`/`operation`). **Expected:** the job result has `plan` and `validation` (`passed: true`, checks `RPLAN.1`-`RPLAN.7`). `plan.questions` has one question per contract KPI (`Q-R1`, … with `source` `contract.kpis[<name>]`) and one per contract dimension. A visual such as "Net Revenue by Month" has the KPI's question in `question_ids`, and every question appears in some visual's `question_ids` (or in `unanswered_questions`). Visual ids are `v1`, `v2`, …; at most 8 visuals per page; every bound measure, column and hierarchy is one of the model's. The run is still `MODEL_APPROVED`. `python -m app.cli runs pull <run_A>` copies `ReportPlan.json` and `reports/ReportPlan.v1.json` locally to inspect. `call("GET", "/runs/{run_A}")` shows `ReportPlan` as `draft` under `artifacts`. Calling the job again with nothing changed gives the same version 1 and makes no new model call.
2. 🌐 **Bad reference caught.** `call("GET", "/runs/{run_A}/report/plan")` re-validates the saved file, so a hand edit shows up: against `RUN_STORE=file`/`ARTIFACT_STORE=local` locally (or by writing the file into OneLake with `app/connectors/onelake_files.py` against the control Lakehouse), change a visual's `{"measure": "Net Revenue"}` to `{"measure": "Net Revenue X"}`, then call the read again (or `python -m app.cli report validate runs\<run_A>` against a local copy, exit 1). **Expected:** `RPLAN.2` fails with `v1.fields.values[0]: measure 'Net Revenue X' is not in the approved model` (the visual and the field). Restore the file afterwards. Also try `"type": "pieChart"` (`RPLAN.5` lists the allowed types) and giving two visuals the same id (`RPLAN.4`).
3. 🌐 **No invented measures.** On a second run (run B) in `MODEL_APPROVED` whose contract has a KPI the model has no measure for (for example, a fixture contract with an extra KPI "Customer Churn" saved as `BusinessContract.yaml`), run the job for `POST /runs/{run_B}/report/plan`. **Expected:** the KPI's question is in `plan.unanswered_questions` with the reason "The approved model has no measure for KPI 'Customer Churn'", no visual answers it, `ModelSpec.yaml` has no new measure, `data_gaps.jsonl` has an entry for it and the run is `NEEDS_INPUT` with `resume_state: MODEL_APPROVED` ("Return to the model or drop the question"). `ReportPlan` is registered as `needs_input`.
4. 💻 **Errors.** A `job:` operation called through `call(...)` directly (`as_job=False`) is `400 BAD_REQUEST`. Run through the job path: in another state `409 WRONG_STATE`; without an approved model `404 NOT_FOUND`; `ModelSpec.yaml` edited after approval `409 STALE_VERSION`; the model's answer unusable twice `502 LLM_INVALID_OUTPUT` with nothing saved.
5. 💻 **Offline checks.** `python -m app.manual_tests.t601_offline_checks`. **Expected:** `All checks passed.` (the questions, RPLAN.1-RPLAN.7, the retry, unanswered questions, `POST`/`GET /report/plan`, caching and the error cases — this exercises the business logic through the local FastAPI app, confirmed unaffected).
6. 💻 **Clean up.** Restore any hand-edited `ReportPlan.json` (or run the job again), and delete the test runs' folders.

**Not run by Claude:** the live steps (1-4), which need a live Fabric tenant and the model service. Claude ran step 5 offline.

### T-602 — PBIR generator and sandbox publish

> **Fabric re-test (2026-10-21).** Logic unchanged; confirmed wired through the dispatcher: `POST /runs/{id}/report/generate` is `job:` ([CONTRACTS.md#fabric-operations](tasks/CONTRACTS.md#fabric-operations)). The staging-then-publish-then-rename sequence is unchanged; `items_client()` defaults to T-002's token provider (never OBO), confirmed consistent with T-501/T-504.

**Before you start:** run A from T-601 in `MODEL_APPROVED` with a valid `ReportPlan.json` (no unanswered questions) and its model `Nexora_<first 8 characters of the run id>` deployed in the sandbox workspace (validating the model, T-504, deploys it). `SANDBOX_WORKSPACE_ID` set in `nexora_settings` (or `.env` for a local-store run). For the 2024 filter in step 1 the plan needs a page filter such as `{"table": "Date", "column": "Year", "values": [2024]}`: add it by hand under `pages[0].filters` in `ReportPlan.json` before generating (same access note as T-601 step 2).

1. 🌐 **Visible report.** Run the job for `POST /runs/{run_A}/report/generate`. **Expected:** the job result has `version: 1`, `operation: "created"`, a `report_item_id`, and `files` listing `definition.pbir`, `definition/report.json`, `definition/version.json`, `definition/pages/pages.json`, `definition/pages/p1/page.json`, `definition/pages/p1/visuals/v1/visual.json`, …. The run is now `REPORT_GENERATING`. In the sandbox workspace the report `Nexora_<first 8 characters>_Report` exists; open it: the pages, titles and visuals match `ReportPlan.json`, the visuals show data (the first load can take a moment), and the page filter (2024) is applied in the filters pane.
2. 🌐 **Drill works.** On "Net Revenue by Month" (a line chart on the Calendar hierarchy) use the drill-down arrow. **Expected:** the chart goes Year → Quarter → Month, as the plan describes.
3. 💻 **Deterministic.** Run the job again, pull both versions (`python -m app.cli runs pull <run_A>`, keeping a copy of `report/v1` first), then compare the folders. **Expected:** no difference (the run stays `REPORT_GENERATING`). If you have validated the report in between (T-603), ignore the `renders` folder, which validation adds to a version and which is not part of the report.
4. 🌐 **No duplicate.** After step 3. **Expected:** the second result has `operation: "updated"` and the same `report_item_id`; the workspace still has one report with that name.
5. 🌐 **Rebinding.** Note the model's item id from the pulled `ReportPublish.json` (`semantic_model_item_id`). Delete the model `Nexora_<first 8 characters>` in the workspace and deploy it again (`python -m app.cli deploy model runs\<run_A>\model\v<n> --name Nexora_<first 8 characters>`, local entry point, T-501), then generate again. **Expected:** `definition.pbir` of the new version carries the new model's id (`semanticmodelid=…`) and the report opens on the new model.
6. 💻 **Errors.** Change a visual's `"type"` to `"pieChart"` in `ReportPlan.json`: the generate job gives `422 VALIDATION_ERROR` naming `v1` and listing the allowed types, with nothing published. A wrong state gives `409 WRONG_STATE`; no plan `404`; the model missing from the sandbox `404`; a Fabric rejection `502 UPSTREAM_ERROR` with Fabric's message and no new `report\v<n>` folder; calling it as a `udf:` (`as_job=False`) is `400 BAD_REQUEST`. Restore the plan afterwards.
7. 💻 **Offline checks.** `python -m app.manual_tests.t602_offline_checks`. **Expected:** `All checks passed.` (layout, `$schema`s, determinism, the hierarchy and drill levels, filters, interactions, the visual types, the item client's create and update, the endpoint and its errors).
8. 💻 **Clean up.** Delete the report and the model from the sandbox workspace and the test runs' folders.

**Not run by Claude:** the live steps (1, 2, 4, 5 and the Fabric error in 6), which need a live Fabric tenant and the sandbox workspace. Claude ran step 7. The generated files follow Microsoft's published PBIR schemas, but nothing here validates them against the live JSON schemas: if Fabric or Power BI rejects a file, the message names it, and the generator (`app/report/pbir_generator.py`) is what to fix.

### T-603 — Report validators and Gate G5

> **Fabric re-test (2026-10-21).** Logic unchanged; confirmed wired through the dispatcher: `POST /runs/{id}/report/validate` and `POST /runs/{id}/report/repair` are `job:` ([CONTRACTS.md#fabric-operations](tasks/CONTRACTS.md#fabric-operations)). Renders are written to `Files/runs/<id>/report/v<n>/renders/` of `nexora_control` and shown by T-604 through the `get_run_file` udf, not a direct file path.

**Before you start:** run A from T-602 in `REPORT_GENERATING` (the report generated and published, the model `Nexora_<first 8 characters>` in the sandbox), the job's `pbi`-audience token confirmed sufficient for `executeQueries` and refresh (T-504 step 1). The page export needs a **Fabric or Premium capacity** (a trial capacity works): without one `G5.RENDER` is a warning, not a pass.

1. 🌐 **Passing report.** Run the job for `POST /runs/{run_A}/report/validate`. **Expected:** `report.passed: true`, `G5.SCHEMA` pass, one `G5.BIND` and one `G5.QUERY` pass per visual (a `no data` warning is possible if a visual returns no rows), and one `G5.RENDER` pass per page; `python -m app.cli runs pull <run_A>` shows `report\v<n>\renders\<page>.png` for each page (open one: it shows the page as Power BI renders it). The run stays `REPORT_GENERATING`. Each `G5.QUERY` check has the DAX it ran in `evidence.query`.
2. 🌐 **Binding failure repaired narrowly.** Edit the current version's `definition\pages\p1\visuals\v1\visual.json` (same access note as T-601 step 2) to change `"Net Revenue"` (in the Y projection and its `queryRef`) to `"Net Revenue X"`, then validate again. **Expected:** `report.passed: false`, `G5.BIND` fails for `visual:v1` naming `Net Revenue X` (`evidence.defect: "report"`), that visual's `G5.QUERY` and the page's `G5.RENDER` are `skipped`, and the run is `REPORT_FAILED`. Then run the job for `POST /runs/{run_A}/report/repair`. **Expected:** `outcome: "passed"`, `final_state: "REPORT_GENERATING"`, one attempt; the new `report\v<n+1>` differs from `v<n>` only in `definition\pages\p1\visuals\v1\visual.json` (ignoring `renders`), the report in the workspace shows the visual again, and a validation entry with `repair_attempt: 1` passed. This is the "Report binding failure" scenario.
3. 🌐 **Model defect routed back.** Break a measure in the **deployed** model (open `Nexora_<first 8 characters>` in the workspace's web modeling view and change `Net Revenue`'s DAX to something invalid such as `SUM(Sales[Nope])`). Then validate the report again. **Expected:** `G5.QUERY` fails for the visuals using it with `evidence.defect: "model"` and the engine's message, the run is `MODEL_GENERATING` (`call("GET", "/runs/{run_A}")`: the history shows `REPORT_FAILED` then `MODEL_GENERATING` with "Model defect found by report validation: …"), `ValidationReport.json` has a failed `G4.DAX` entry for the model (`REPORT_QUERY_FAILED`), and `call("GET", "/runs/{run_A}/model/review")` shows Gate G4 not passed until you validate the model again. No new `report\v<n>` is written. Restore the measure by validating the model, which redeploys it.
4. 🌐 **Export unavailable.** On a workspace without capacity (or by temporarily using a run whose workspace isn't on one), validate. **Expected:** `G5.RENDER` is a `warning` "export unavailable: …" (no PNGs), the other checks decide, and `report.passed` can still be `true`.
5. 💻 **Errors.** Validate or repair in another state gives `409 WRONG_STATE`; validate before `report/generate` gives `404`; the model missing from the sandbox gives `404`; repair with no failed check gives `409 WRONG_STATE` "Nothing to repair"; a defect outside one visual or page (for example deleting `definition\version.json`) fails `G5.SCHEMA` on `report`, and repair escalates it (`user_input_only`, `NEEDS_INPUT`, resume `REPORT_FAILED`); either operation called as a `udf:` is `400 BAD_REQUEST`.
6. 💻 **Offline checks.** `python -m app.manual_tests.t603_offline_checks`. **Expected:** `All checks passed.` (the four checks, the queries built per visual, the warnings, the narrow repair, the model defect route, escalation and the error cases).
7. 💻 **Clean up.** Delete the report and the model from the sandbox workspace and the test runs' folders.

**Not run by Claude:** the live steps (1-4), which need a live Fabric tenant, the sandbox workspace and Power BI. Claude ran steps 5 and 6 offline with fakes for Fabric and Power BI.

### T-604 — Report review and approval UI

> **Fabric rework (2026-10-21).** The backend (services, gates, approvals) is T-601/T-602/T-603's, unchanged, confirmed wired through the dispatcher: `GET /runs/{id}/report/review` is `udf:`, `POST /runs/{id}/approvals` on `Report` is `udf:` except `REGENERATE`, which is `job:` (`_approve_host`), matching [CONTRACTS.md#fabric-operations](tasks/CONTRACTS.md#fabric-operations). `GET /runs/{id}/files/{path}` is now the udf `get_run_file`: same path rules (no absolute path, `..`, backslash or colon; only files inside the run folder), body `{"content_type", "base64", "size"}`; a file over the UDF response limit (fixed in this pass: 20 MB raw, under the 30 MB response ceiling in `fabric-hosting.md` once base64 and the JSON envelope are counted) is `413 FILE_TOO_LARGE`, so the page falls back to the "open in Power BI" link. The UI is new: a **Report** tab (`rayfin-workload/src/pages/ReportPage.tsx`) on the **Nexora Run** item editor, replacing `rayfin-ui`'s `/runs/[id]/report`. `src/lib/report.ts` (display rules, ported in T-104/T-604) is unchanged; `src/lib/api.ts` gained `generateReportPlan`, `generateReport`, `validateReport`, `repairReport` (jobs), `getReportPlan`, `getReportReview` (udf) and `getRunFile` (udf, used for each page's render instead of an authenticated `<img>`). The page adds a "Build the report" section (plan/generate/validate/repair, mirroring the Plan and Model tabs' own "Prepare"/"Build" sections) ahead of the review data, since T-604 is also where a reviewer drives those earlier M6 stages from Rayfin for the first time. `npm run typecheck` and `npm test` (60 tests) pass clean; there is no dev server for `rayfin-workload` yet (see its README), so the 🌐 steps need this project dropped into the real Extensibility Toolkit Starter-Kit first.

**Before you start:** run A from T-603 in `REPORT_GENERATING`, validated (passing, PNGs in `report\v<n>\renders\`; without a capacity the pages show the placeholder instead). Put **your own UPN** in `Files/config/roles.yaml` of `nexora_control` under `project_reviewer` and `business_owner` (restore it afterwards). Open run A's **Report** tab in Rayfin (Fabric portal).

1. 🌐 **Review screen.** **Expected:** the summary shows the state, **Gate G5 passed**, the version and the report hash; each page shows its rendered image, fetched through `get_run_file` and shown as a data URL (the placeholder "Render unavailable — open in Power BI" with a link if there is no capacity, or if the render is over the UDF response limit) next to the questions it answers; each visual shows its title, type, the questions it answers and **Binding: Passed** / **Query: Passed** (a "no data" warning shows its message).
2. 🌐 **Single-visual regenerate.** Click **Regenerate** on "Net Revenue by Month", type "use a column chart" and click **Regenerate**. **Expected:** a notice says it was regenerated; the version goes up by one; pulling both versions shows only `definition/pages/p1/visuals/v1/visual.json` changed (ignoring `renders`); the chart is now a column chart in the report and in the new render; Gate G5 ran again (a new validation entry); the history shows a REGENERATE with the target and your guidance.
3. 🌐 **Approve.** Click **Approve as Project reviewer**. **Expected:** the run is still `REPORT_GENERATING`, the project reviewer shows "Approved by <you>", the business owner shows **Pending**. A user without a role sees that role's Approve disabled with "Only a business_owner can approve as business_owner." Then **Approve as Business owner** (a separate click, if you hold both roles). **Expected:** the run becomes `REPORT_APPROVED`, the history has one record per role with your UPN, and `call("GET", "/runs/{run_A}/approvals")` ends with those `Report` records on the current hash.
4. 🌐 **Invalidation.** On a fresh run, approve as Project reviewer, then regenerate a visual (or hand-edit `definition\pages\p1\page.json` of the current version, same access note as T-601 step 2). Reload the tab. **Expected:** the project reviewer shows **Invalidated: report changed**, the history entry is marked "invalidated: report changed", the page warns that the latest validation is of another version (after a hand edit), and approving as the business owner gives `409 GATE_FAILED` (`G5.CURRENT`) until you click **Validate again**.
5. 🌐 **Traversal blocked.** In the `nexora_api` test pane, call `call("GET", "/runs/{run_A}/files/..%2F..%2Fconfig%2Froles.yaml")`, then the same with an absolute path and with a backslash (`..%5Csecret.txt`). **Expected:** `404` each time, and no file content. Also `call("GET", "/runs/{run_A}/files/report/v1/renders/p1.png")` gives `200` with `{content_type: "image/png", base64, size}`, and a path outside the run folder gives `404`.
6. 🌐 **M6 exit.** Using Rayfin only: Generate plan, Generate report, Validate, and the two approvals. **Expected:** the run is `REPORT_APPROVED`, `call("GET", "/runs/{run_A}/artifacts")` shows `PbirReport` and `ReportPlan` as `approved`, and the report `Nexora_<first 8 characters>_Report` exists in the sandbox workspace.
7. 💻 **Offline checks.** `python -m app.manual_tests.t604_offline_checks`. **Expected:** `All checks passed.` (the review data, the file endpoint and its traversal cases, the placeholder, partial and complete approval, one user with both roles, role and hash checks, Gate G5, invalidation, Reject, regenerating a visual and a page, an invented measure refused, a model defect routed back, and the 403/404/409/422 cases — this exercises the business logic through the local FastAPI app; the dispatcher's own `_run_file` (the `get_run_file` udf's `{content_type, base64, size}` shape and its `413` for an oversized file) was checked directly in this session against the local stores).
8. 💻 **Typecheck and display-rule tests.** `cd rayfin-workload; npm run typecheck; npm test`. **Expected:** `tsc --noEmit` reports nothing, and all 60 tests pass (`node --test`) — verified in this session.
9. 💻 **Clean up.** Restore `Files/config/roles.yaml`, delete the report and model from the sandbox workspace.

**Not run by Claude:** the live and browser steps (1-6), which need a live Fabric tenant and the Rayfin workload deployed into the real Starter-Kit, plus, for the images, a Fabric or Premium capacity. Claude ran steps 7 and 8.

---

## M7 — Operational controls

### T-701 — Impact analysis and stale marking

> **Fabric re-test (2026-10-21).** Unchanged business logic; confirmed wired through the dispatcher: `POST /runs/{id}/return` and `GET /runs/{id}/impact` are `udf:`, `POST /runs/{id}/regenerate-stale` is `job:`, matching [CONTRACTS.md#fabric-operations](tasks/CONTRACTS.md#fabric-operations). There is still no Rayfin screen for this (not built in the first pass either); the steps below use the `nexora_api` test pane's `call(method, path, body=…)` for `udf:` operations and `runJob`-equivalent job starts (`call("POST", "/runs/{id}/jobs", body={"operation": "POST /runs/{id}/regenerate-stale", "body": {}})`, then poll `GET /runs/{id}` and read `GET /runs/{id}/jobs/{job_id}`) for the `job:` one.

**Before you start:** run A from M6, `REPORT_APPROVED`, with its model, report and approvals (the exit run of T-604). Put **your own UPN** in `Files/config/roles.yaml` of `nexora_control` under `business_owner`, `bi_engineer` and `project_reviewer` (restore it afterwards). `<id>` is run A's `run_id`; the contract hash for edits is `artifacts.BusinessContract.hash` in `call("GET", "/runs/{id}")`.

1. 🌐 **Return and change.** `call("POST", "/runs/{id}/return", body={"to": "REQUIREMENT_ANALYSIS", "reason": "Exclude tax"})`. **Expected:** `200`, state `CREATED`, `contract.status` `draft` with the version one higher, the reason in the history, and a `returned` line in `Files/runs/<id>/returns.jsonl` of `nexora_control` with a baseline of every artifact; every earlier version is still there (`python -m app.cli runs pull <id>` to inspect locally). Then `call("PUT", "/runs/{id}/contract/kpis/Net Revenue", body={"formula_intent": "<the old text> and tax", "expected_hash": "<hash>"})` (**Expected:** `200`), and `call("POST", "/runs/{id}/approvals", body={"artifact": "BusinessContract", "action": "ACCEPT", "expected_hash": "<new hash>"})` (**Expected:** `200`, state `REQUIREMENT_APPROVED`).
   - `call("POST", "/runs/{id}/return", ...)` on a run in `DEPLOYING` (set with `python -m app.cli runs transition <id> DEPLOYING` on a throw-away run with no stale artifact) gives `409 WRONG_STATE`.
2. 🌐 **Impact listed.** `call("GET", "/runs/{id}/impact")`. **Expected:** `changed` is `BusinessContract` with `kpi:Net Revenue`; `stale` lists `SourceResolution`, `Feasibility` and `ExecutionPlan` (`kpi:Net Revenue`), `ModelSpec` and `TMDLModel` with the Net Revenue measure (and any measure whose DAX uses it), the TMDL file of its table, and `ReportPlan` / `PbirReport` with exactly the visuals that use those measures. Unrelated measures, tables and visuals are not listed.
3. 🌐 **Blocked.** `python -m app.cli runs transition <id> DEPLOYING` (local CLI against the Fabric stores, `RUN_STORE=fabric_sql`, `ARTIFACT_STORE=onelake`), then `echo $LASTEXITCODE`. **Expected:** exit `1`, naming the stale artifacts. `call("POST", "/runs/{id}/jobs", body={"operation": "POST /runs/{id}/report/plan", "body": {}})` then running the job gives `409 STALE_ARTIFACTS` with `details.stale` (once the run is in `MODEL_APPROVED`).
4. 🌐 **Targeted regeneration, one stage at a time.** Start `POST /runs/{id}/regenerate-stale` as a job and approve between calls (Plan → Model → Report, as the first build's steps did); compare `plans/`/`model/v<n>`/`report/v<n>` pulled locally to confirm only the stale objects changed. **Expected:** ends `REPORT_APPROVED` with `GET /runs/{id}/impact` `{"changed": [], "stale": [], "open_return": null}` and the return resolved.
5. 💻 **Offline checks.** `python -m app.manual_tests.t701_offline_checks`. **Expected:** `All checks passed.` (return states and unlocking, the baseline log, the object-level impact for a changed, a deleted and a reworded KPI and for plan changes, stale marking, the deployment and stage guards, regeneration of the model and the report with the scope guard, approvals that don't survive a return, and the 404/409/422 cases — this exercises the business logic through the local FastAPI app, since it's unchanged from the first build).
6. 💻 **Earlier checks still pass.** `python -m app.manual_tests.t101_offline_checks`, `t102_dispatch_offline_checks` and `t604_offline_checks`. **Expected:** `All checks passed.` for each.
7. 💻 **Clean up.** Restore `Files/config/roles.yaml`, delete the test runs and their models/reports from the sandbox workspace.

**Not run by Claude:** steps 1-4, which need a live Fabric tenant, the home workspace and the `nexora_api` test pane. Claude ran steps 5 and 6.

### T-702 — Promotion deployment, smoke tests and rollback

> **Fabric rework (2026-10-21).** Gate G6, the release approval, the hash checks, the backup, the deploy, the smoke tests and the rollback logic are kept from the first build. What's new: the deployment **is** the job (`POST /runs/{id}/deploy` is `job:`, [CONTRACTS.md#fabric-operations](tasks/CONTRACTS.md#fabric-operations)) — reserve it with `call("POST", "/runs/{id}/jobs", body={"operation": "POST /runs/{id}/deploy", "body": {"target_workspace_id": "<target>"}})`, start `nexora_stage_runner`, then poll `GET /runs/{id}` (`state`, `active_job`, `deployment`); the target lock is a row in `dbo.nexora_locks` instead of a lock file; every Fabric and Power BI call runs as the job's own user (`deps.promotion_items_client()`, T-002, no OBO); a cancelled or crashed job leaves the run `DEPLOYING` with `next_action: "Resume rollback"` — a new deploy job in that state only resumes the rollback from the saved backup.

**Before you start:** run A from M6/T-701 in `REPORT_APPROVED` (validated, approved by both roles), and a **second workspace** you can access that is **not** the sandbox (call its ID `<target>`; a dev or test workspace on a capacity, where you can create items and the Lakehouse of run A is readable). Put **your own UPN** in `Files/config/roles.yaml` of `nexora_control` under `release_approver` (restore it afterwards). `<id>` is run A's `run_id`.

1. 🌐 **Gate G6 refuses.** Start the deploy job with `{"target_workspace_id": "<target>"}` before any release approval. **Expected:** `409 GATE_FAILED`, `failed_checks` is `["G6.4"]`, nothing in the target. Then with the sandbox's own workspace id: **Expected:** `409 GATE_FAILED` with `G6.1` (the target is the sandbox).
2. 🌐 **Unauthorized target.** Deploy to a workspace you can't access (or a random GUID). **Expected:** `409 GATE_FAILED` with `G6.1` and nothing deployed (the run stays `REPORT_APPROVED`).
3. 🌐 **Release approval.** `call("POST", "/runs/{id}/approvals", body={"artifact": "Release", "action": "ACCEPT", "expected_hash": "<hash>"})`. The hash is the SHA-256 of `{"PbirReport": <PbirReport hash>, "TMDLModel": <TMDLModel hash>}` (sorted keys, no spaces); send any value first to get `409 STALE_VERSION` with `current_hash`, then use that. Without the role: `403`. **Expected with the role:** `200`; repeating it gives `409 ALREADY_APPROVED`.
4. 🌐 **Promote.** Deploy to `<target>`. **Expected:** the job runs to completion; `deployment.items` lists the `SemanticModel` and the `Report` with the approved hashes (`operation: created`), `deployment.smoke_tests` has `SMOKE.REFRESH`, one `SMOKE.MEASURE:<name>` per measure and `SMOKE.REPORT`, all `pass`, `outcome` is `completed`, `job_instance_id` is recorded, and `Files/runs/<id>/deploy/backup-<ts>/backup.json` exists in `nexora_control`. In the target workspace `Nexora_<first 8 characters>` and `Nexora_<first 8 characters>_Report` exist and the report opens on the target's model.
5. 🌐 **Rollback.** On a second `REPORT_APPROVED` run with a release approval, make a smoke test fail (for example remove the target workspace's access to run A's Lakehouse) and deploy. **Expected:** `ROLLED_BACK`, `deployment.error` names the failure, a new model/report was deleted from the target and one that existed before is restored to its previous definition (compare with the backup). Fix the cause and deploy again: **Expected:** `COMPLETED`.
6. 🌐 **Hash enforced.** On another approved run with a release approval, edit a TMDL file of the approved version in `Files/runs/<id>/model/v<n>/` (`python -m app.cli runs pull`, edit, re-upload, or edit directly in OneLake), then deploy. **Expected:** `409 HASH_MISMATCH` naming `TMDLModel`, nothing deployed.
7. 🌐 **Busy target.** Start two deployments of different runs to the same target. **Expected:** the second fails with `409 TARGET_BUSY` (a row in `dbo.nexora_locks`, or the file lock locally), and the lock row/file is gone after the first finishes.
8. 🌐 **Cancelled job.** Cancel a deployment job from the Monitoring hub after the model is deployed but before the report. **Expected:** the run stays `DEPLOYING`, `next_action` is "Resume rollback" (once `GET /runs/{id}` reconciles the crashed job as `failed`). Start `POST /runs/{id}/deploy` again (the same or any target body — it's ignored). **Expected:** only the rollback runs (nothing is redeployed), the target is restored, the lock is released and the state becomes `ROLLED_BACK`.
9. 💻 **Offline checks.** `python -m app.manual_tests.t702_offline_checks`. **Expected:** `All checks passed.` (the release approval and its roles, G6.1-G6.4, the hash check, the lock — now `app.store.locks` — and a stale lock, the backup, a completed promotion with its manifest and smoke tests, rollback of a new deployment, rollback to the previous definitions when the report fails, a failing rollback, blank and failing measures, and the Fabric client calls; this exercises the business logic through the local FastAPI app, since it's unchanged from the first build). The new resume-rollback path (step 8) isn't exercised offline — it needs a real cancelled Fabric job.
10. 💻 **Earlier checks still pass.** `python -m app.manual_tests.t501_offline_checks`, `t701_offline_checks` and `t102_dispatch_offline_checks`. **Expected:** `All checks passed.` for each.
11. 💻 **Clean up.** Restore `Files/config/roles.yaml`, delete the test items from `<target>` and the sandbox and the test runs.

**Not run by Claude:** steps 1-8, which need a live Fabric tenant and a real deployment target. Claude ran steps 9 and 10.

### T-703 — Audit trail and traceability view

> **Fabric rework (2026-10-21).** The backend (deriving and verifying events, the timeline, the trace) is unchanged from the first build; confirmed wired through the dispatcher: `GET /runs/{id}/audit` and `GET /runs/{id}/trace` are `udf:`, matching [CONTRACTS.md#fabric-operations](tasks/CONTRACTS.md#fabric-operations). `events.jsonl` now goes through the `ArtifactStore` (`append_line`, leased) instead of a local `Path`, so a UDF and a job recording events at the same time stay consistent. The UI is new: an **Audit** tab and a **Trace** view (opened from a "Trace" button on each visual of the **Report** tab) on the **Nexora Run** item editor, replacing `rayfin-ui`'s `/runs/[id]/audit` and `/runs/[id]/trace/[visualId]`. `src/lib/audit.ts` (display rules) is unchanged; `src/lib/api.ts` gained `getAudit` and `getTrace` (both `udf`). `npm run typecheck` and `npm test` (60 tests) pass clean; there is no dev server for `rayfin-workload` yet (see its README), so the 🌐 steps need this project dropped into the real Extensibility Toolkit Starter-Kit first.

**Before you start:** run A taken to `COMPLETED` (T-702), with a validated model, an approved report and a release. `<id>` is run A's `run_id`. Open run A's **Audit** tab in Rayfin (Fabric portal), and the **Report** tab for step 2.

1. 🌐 **Timeline.** **Expected:** "the chain is intact", then every event of the run, oldest first: the state changes (including `REPORT_APPROVED → DEPLOYING` and `→ COMPLETED`), each approval with who gave it, the role, the time and the artifact version and hash, gate results (G1 to G6), validations and the deployment outcome with the deployed hashes.
2. 🌐 **Trace.** On the Report tab, click **Trace** on a visual. **Expected:** the steps *Visual → Measure → DAX → Approved KPI → source columns → Prompt*; the measure with its DAX and model version, the KPI definition with the contract version, who approved it and when, each source column with its profile evidence (rows, nulls, distinct values, range, the queries) and the original prompt, without leaving Rayfin; **Back to the report review** and **Audit trail** links work.
3. 🌐 **Gaps are shown.** Rename the approved contract version file under `Files/runs/<id>/contracts/` of `nexora_control` and reopen the trace. **Expected:** a "Gaps in the trace" box says "Contract version <n> is missing…" and the KPI definition is empty; the rest of the trace still shows. Rename the file back.
4. 🌐 **Tamper detection.** Copy `Files/runs/<id>/events.jsonl`, then change one word in line 3 of the original and reopen the Audit tab. **Expected:** a red banner "Audit log tampered with" naming event **4** as the first break, and `call("GET", "/runs/{id}/audit")` returns `chain_valid: false`, `first_broken_seq: 4`. Restore the copy; **Expected:** the chain is intact again.
5. 🌐 **Paging.** `call("GET", "/runs/{id}/audit", {"offset": 0, "limit": 5})` in the test pane. **Expected:** 5 events (`seq` 1 to 5) and the full `total`; in Rayfin a long log shows 200 events a page with **Earlier events** / **Later events**.
6. 💻 **Offline checks.** `python -m app.manual_tests.t703_offline_checks`. **Expected:** `All checks passed.` (the events for every source, the hash chain, no duplicates on a second look, paging, the trace of two visuals, the explicit gaps, an edited, removed, unreadable and reordered line, and the 404/422 cases — now through `ArtifactStore.append_line`, with the test's own `get_artifact_store` pointed at its temporary folder).
7. 💻 **Typecheck and display-rule tests.** `cd rayfin-workload; npm run typecheck; npm test`. **Expected:** `tsc --noEmit` reports nothing, and all 60 tests pass — verified in this session.
8. 💻 **Clean up.** Restore anything you renamed or edited, delete the test runs and their items from the workspaces.

**Not run by Claude:** the live steps (1-5), which need a live Fabric tenant and the Rayfin workload deployed into the real Starter-Kit. Claude ran steps 6 and 7.

### T-704 — Observability

> **Fabric rework (2026-10-21).** The spans, metrics, JSON logs, redaction and pricing are unchanged from the first build. What's new: there's no HTTP middleware in Fabric, so `app/fabric/dispatch.py:dispatch()` binds `run_id`/stage itself and records the span for a non-GET `/runs/{id}/...` call, and `app/fabric/job.py:run_job` additionally binds `job_id`; the default sink inside a Fabric job or UDF is now the home workspace's Eventhouse (`OTEL_EXPORTER=eventhouse`, `NEXORA_EVENTHOUSE_URI`/`NEXORA_EVENTHOUSE_DATABASE`, KQL database `nexora_telemetry`, tables `nexora_logs`/`nexora_spans`/`nexora_metrics`); `console` and `azuremonitor` are unchanged and stay available for local development. The job's notebook now exits with `{"run_id", "operation", ...}` so the Monitoring hub entry can be matched to the run.

**Before you start:** the home workspace deployed (T-005) with its Eventhouse, `OTEL_EXPORTER` left unset in `nexora_settings` (so a job/UDF defaults to `eventhouse`) or set explicitly, and `config/model_pricing.yaml` published to `Files/config/` with the price of your deployment (`AZURE_OPENAI_DEPLOYMENT`'s name, `input_per_1k`/`output_per_1k` in USD per 1,000 tokens; without it the cost isn't recorded and one warning names the deployment). Put a phrase of your own in the prompt you test with, for example `zebra-quartz`.

1. 🌐 **Correlated.** From Rayfin, create a run, compile it and harvest it. In `nexora_telemetry`, run `nexora_spans | where run_id == "<id>" | project name, duration_ms, outcome`. **Expected:** the UDF calls (`stage.compile`, `stage.run`, …) and both job stages (`stage.compile`, `stage.harvest`), each with a duration; child spans `stage.compile.node.llm_compile`/`node.validate_schema` are there too. `nexora_logs | where run_id == "<id>"` shows `job_id` on the job's own lines.
2. 🌐 **Costs and retries.** Run a model repair on a run with a failing measure. **Expected:** `nexora_metrics` has `nexora.retries` for it, and `nexora.llm.tokens.*`/`nexora.llm.cost` for its model calls, all tagged with `run_id`.
3. 🌐 **No prompt leakage.** Search `nexora_logs` for your phrase. **Expected:** not found; only `len`/`sha256` of the instructions, input and output appear, plus the token counts and `cost_usd`.
4. 🌐 **Monitoring hub.** Open the compile job in the Fabric Monitoring hub. **Expected:** its exit value (or output) shows `run_id` and `operation`.
5. 🌐 **Azure Monitor alternative.** Set `OTEL_EXPORTER=azuremonitor` and `APPLICATIONINSIGHTS_CONNECTION_STRING`, repeat step 1. **Expected:** the same spans and metrics in Application Insights (`traces | where customDimensions.run_id == "<id>"`).
6. 💻 **Console in local development.** Locally with `OTEL_EXPORTER=console`, run a CLI-driven stage script (for example `python -m app.cli contract validate ...` after a compile, or any `app.test_*` script). **Expected:** spans with the `run_id` on stdout.
7. 💻 **Offline checks.** `python -m app.manual_tests.t704_offline_checks`. **Expected:** `All checks passed.` (the span and node spans with their attributes, the correlated JSON logs, no span outside a run, outcomes, retries and attempts, validation outcomes, token and cost metrics, a missing price, no content in any log line, exporters that fail, the settings including `eventhouse`, the dispatcher's own run_id/stage binding and `run_job`'s job_id, the three eventhouse exporter adapters against a fake sink, and the header on outbound calls). Lines such as `ConnectionError: exporter unreachable` printed by it come from a failing exporter the test installs on purpose.
8. 💻 **Earlier checks still pass.** `python -m app.manual_tests.t202_offline_checks`, `t505_offline_checks` and `t102_dispatch_offline_checks`. **Expected:** `All checks passed.` for each.
9. 💻 **Clean up.** Leave `OTEL_EXPORTER` as you found it; remove any test runs' folders left under the project's local `runs/`.

**Not run by Claude:** steps 1-6, which need a live Fabric tenant, its Eventhouse (or Application Insights) and the live model. Claude ran steps 7 and 8.

### T-705 — Security hardening

> **Fabric rework (2026-10-21).** The payload guard and the LangSmith block are unchanged. What's new: `app/security/secrets.py:read_vault_secret` reads `azure-openai-api-key` with `notebookutils.credentials.getSecret` inside a Fabric job (and keeps `DefaultAzureCredential` locally); `entra-api-client-secret` is no longer needed. Run authorization moved into the dispatcher (`app/fabric/dispatch.py:_authorize`): the creator may always act; a **job** lists its own user's Fabric workspaces and writes the decision (`app.security.run_access.can_access`); a **UDF** has no such token, so it only reads the cached decision (`cached_decision()`) — no fresh row is `403 ACCESS_CHECK_REQUIRED` (the UI then runs `job:check_access` and retries), a fresh "not allowed" row is `403 FORBIDDEN` immediately. The cache itself is `default_cache()`: the in-memory one locally (`RUN_STORE=file`), or `dbo.nexora_run_access` (`RUN_STORE=fabric_sql`) in Fabric, same 5-minute TTL either way.

**Before you start:** the home workspace deployed (T-005), `KEY_VAULT_URL` set in `nexora_settings` pointing at a vault with the secret `azure-openai-api-key`, and the job's identity (or the workspace identity, whichever Key Vault grants — confirm and record in fabric-hosting.md) allowed to read it. Never paste a real secret into chat or commit it.

1. 🌐 **Secrets from Key Vault.** Make sure `nexora_settings` and `.env` have no `AZURE_OPENAI_API_KEY`, and compile a run in Rayfin. **Expected:** it works.
   - Rotation: change `azure-openai-api-key`'s value in the vault, then compile again without restarting anything. **Expected:** the first call after rotation may fail once, but a `401` re-reads the key and retries, so the overall call succeeds.
   - Unreachable vault: point `KEY_VAULT_URL` at a vault the job can't reach and compile. **Expected:** the job fails with `CONFIG_ERROR` naming `KEY_VAULT_URL` and the host, never a secret value. Restore the URL.
2. 🌐 **Run isolation.** As user B, with no access to user A's workspace, open user A's run's item (or `call("GET", "/runs/<A's run>")`). **Expected:** `403 ACCESS_CHECK_REQUIRED` first (no cached decision yet); the UI (or you, manually) starts `job:check_access` (`call("POST", "/runs/{id}/jobs", body={"operation": "POST /runs/{id}/check_access", "body": {}})`), then retries: **Expected:** `403 FORBIDDEN`, and the run doesn't appear in B's run list.
3. 🌐 **Access granted.** Give user B access to the workspace and repeat `job:check_access` then the read. **Expected:** after the access check, the run opens. Remove B's access again and wait 5 minutes (the row's TTL): **Expected:** B loses access again.
4. 🌐 **Guard blocks rows.** In a notebook (or `python -m app.manual_tests.t705_guard_demo` locally), call the shared model client with a payload containing a list of 10 row dictionaries. **Expected:** `PayloadRejected`, no request sent, and the reason in `nexora_logs`/the console — never the content.
5. 🌐 **Payload log.** Review the payload log for a full run in `nexora_logs` (or the local console). **Expected:** only allowed shapes, with their sizes (`payload_chars`, `payload_shape`), never table values, row data or prompt text.
6. 🌐 **LangSmith blocked.** Set `LANGSMITH_TRACING=true` in `nexora_settings` and start a compile. **Expected:** the job fails with `CONFIG_ERROR` naming the setting. Remove it afterwards.
7. 💻 **Offline checks.** `python -m app.manual_tests.t705_offline_checks`. **Expected:** `All checks passed.` (the vault, the fallback and rotation; the guard on rows, matrices, pasted tables, size, and every real payload of the pipeline; the shared client and its log; run access with the 5-minute cache by tenant and workspace; `422 PAYLOAD_REJECTED`; the LangSmith block; the new `SqlAccessCache` and `default_cache()` selection against a SQLite stand-in for the control database, `cached_decision()`, and the `job:check_access` dispatcher operation, including the fix so a UDF actually benefits from a cached decision instead of always bouncing to `ACCESS_CHECK_REQUIRED`).
8. 💻 **Earlier checks still pass.** `python -m app.manual_tests.t102_dispatch_offline_checks`, `t105_offline_checks` and `t704_offline_checks`. **Expected:** `All checks passed.` for each.
9. 💻 **Clean up.** Remove `LANGSMITH_TRACING` if you set it, delete the test secrets and runs.

**Not run by Claude:** steps 1-6, which need a live Fabric tenant, a real Key Vault and a second account. Claude ran step 7 (which also covers step 4's guard and step 6's LangSmith block offline) and step 8.

### T-706 — End-to-end vertical slice acceptance

This is the acceptance of the whole slice. Record everything in [T-706-evidence.md](tasks/T-706-evidence.md). Use the sandbox workspace and a test "higher" workspace; never production. A defect found here is fixed on this branch in its own commit that names the scenario, and then **every** scenario is run again; a fix that needs a new feature stops the task and becomes a new task in the plan.

**Before you start:** the API (`uvicorn app.api.main:app --reload --port 8000`) and the UI (`cd rayfin-ui; npm install; npm run dev`, <http://localhost:3000>) running, `.env` filled in (model, Fabric, Entra), `SANDBOX_WORKSPACE_ID` set, a sandbox Lakehouse with the `saleslt` sample data and the `nexora_test` fixtures (T-004), a second workspace you can access for the promotion, and your own UPN under every role you need in `config\roles.yaml` (or a colleague per role: business owner, BI engineer, project reviewer, release approver, security reviewer, data engineer). Restore `config\roles.yaml` afterwards.

1. 💻 **Offline rehearsal.** `python -m app.manual_tests.t706_acceptance`. **Expected:** the Definition of Done list with every line ticked (the data stages are seeded; the rest runs through the API), a scenario table with `PASS` on all seven rows, and `All checks passed.`. It takes a few minutes: it runs the suites behind the scenarios as well. `python -m app.manual_tests.t706_acceptance --run-only` runs the integrated run alone.
2. 🌐 **Simple Sales KPI, in Rayfin.** On `/runs/new` choose the sandbox workspace and Lakehouse and enter "Show revenue by month for 2024, from the line totals of the sales orders". Then, using only Rayfin (the run page links to every screen): **Compile** and answer the Revenue question (for example "Net Revenue = SUM of LineTotal excluding cancelled orders", currency USD) and approve the contract as the business owner → **Harvest, profile, analyze relationships**, review and accept the inventory as the data engineer → build the plan (sources, feasibility, bridges if shown, plan), review and approve it as the BI engineer → generate the model and the measures, validate, approve as the BI engineer and the business owner → plan, generate and validate the report, approve as the project reviewer and the business owner → approve the release as the release approver (API: `POST /runs/<id>/approvals`, artifact `Release`) → `POST /runs/<id>/deploy` to the test workspace. **Expected:** the run ends `COMPLETED`; `GET /runs/<id>` shows `deployment.outcome: completed` with the model and the report and their hashes and all smoke tests `pass`; both items exist in the test workspace; `/runs/<id>/audit` shows every stage with who approved it and when; `/runs/<id>/trace/v1` leads from the first visual to the prompt. Note the run ID in the evidence file.
3. 🌐 **Intentional failures inside that run (Definition of Done items 7 and 9).** Before approving the model, break one measure in the deployed TMDL file (`runs\<id>\model\v<n>\definition\tables\<Table>.tmdl`: change a column name in the DAX), **Validate** and then **Repair**. **Expected:** G4 fails with the engine's message and the run is `MODEL_FAILED`; repair makes the next version in one attempt, only that table's file differs (`git diff --no-index --stat runs/<id>/model/v<n> runs/<id>/model/v<n+1>`), the model revalidates and passes. Before approving the report, edit one visual (`runs\<id>\report\v<n>\definition\pages\p1\visuals\v1\visual.json`: change a measure name), **Validate** and **Repair**. **Expected:** G5 fails on that visual (`G5.BIND`), repair regenerates only that `visual.json`.
4. 🌐 **Ambiguous Revenue.** Submit a prompt that says "revenue" with no definition. **Expected:** a blocking question; nothing proceeds (the contract can't be approved, no harvest) until you define Revenue.
5. 🌐 **Invalid relationship.** Using the `nexora_test` fixtures, plan a model that joins the fact to `nexora_test.dim_product_dupes` (duplicate product keys). **Expected:** validation fails on the relationship with the duplicate-key evidence and queries, the run is `MODEL_FAILED`, and the failed section is regenerated or escalated to `NEEDS_INPUT` for input (never silently passed).
6. 🌐 **Many-to-many bridge.** With Product and Promotion (`nexora_test.bridge_product_promotion`): approve the proposed bridge, generate it (`POST /runs/<id>/model/bridges/<bridge id>/generate`). **Expected:** a bridge table with one-to-many relationships is generated and G4.BRIDGE.* checks pass.
7. 🌐 **Unsupported bridge.** Use two entities that need a bridge with no mapping table and no rule in `config\derivation_rules.yaml`. **Expected:** `NEEDS_INPUT` or a data-engineering action; no bridge is guessed.
8. 🌐 **Report binding failure.** As in step 3 (report). **Expected:** only the affected visual or page is repaired.
9. 🌐 **Upstream KPI change.** On a run in `REPORT_APPROVED` (not yet deployed), `POST /runs/<id>/return` to `REQUIREMENT_ANALYSIS`, change the Net Revenue definition and approve the contract again. **Expected:** `GET /runs/<id>/impact` lists the measure, its TMDL table file and the visuals that use it as stale and nothing else; deploying is refused (`STALE_ARTIFACTS`); `POST /runs/<id>/regenerate-stale` stage by stage (see T-701) changes only those objects and the reviews are required again.
10. 🌐 **Review checklist.** Walk through the checklist in the evidence file with the business, BI and security reviewers, on the live run. **Expected:** each reviewer answers every question for their area, "yes", with a note. A "no" is a defect: record it and follow the rule above.
11. 🌐 **Status tracker.** In `.claude\technical-documentation\roadmap-risks-decisions.md`, set each workstream to `Done` once its evidence above is in place. **Expected:** every workstream `Done` and the workflow repeatable end to end (the M7 exit): run steps 2 to 8 once more on a second prompt to show it.
12. 💻 **Clean up.** Restore `config\roles.yaml`, stop the API and UI, delete the test items from the sandbox and the test workspace and the test runs' folders you don't want to keep as evidence.

**Not run by Claude:** steps 2 to 11, which need Rayfin, the live model, Fabric workspaces and people. Claude ran step 1.

---

## Next tasks

T-706 is the last task of the plan.

## Helper scripts

Any script a manual test needs is committed under `app/manual_tests/` (one file per task, named `t<id>_<what>.py`) so every developer runs the same code. Run them as modules from the project root: `python -m app.manual_tests.<name>`. Never keep them as untracked files in the project root or in a personal scratch folder.

| Script | Task | Purpose |
|---|---|---|
| `app/manual_tests/t002_check.py` | T-002 | One sign-in for the Lakehouse and SQL connectors |
| `app/manual_tests/t101_offline_checks.py` | T-101 (first build) | Offline checks of the state table, file store and SQL store logic |
| `app/manual_tests/t101_artifact_store_check.py` | T-101 | `LocalArtifactStore`/`FileLease`: concurrent `append_line`, expired-lease takeover, lease contention, `runs pull` |
| `app/manual_tests/t102_offline_checks.py` | T-102 (first build) | In-process checks of the old FastAPI endpoints, error envelope and CORS |
| `app/manual_tests/t102_dispatch_offline_checks.py` | T-102 | `app.fabric.dispatch`/`app.fabric.job`: health, create, list, validation, not-found, UDF-vs-job host check, full job round trip, `RUN_BUSY` |
| `app/manual_tests/t103_offline_checks.py` | T-103 (first build) | Fake-Fabric checks of the Fabric client, scope binding and scope locking |
| `app/manual_tests/t103_connectors_check.py` | T-103 (first build) | Live check that scope-based connectors reach the Lakehouse and its SQL endpoint |
| `app/manual_tests/t105_offline_checks.py` | T-105 (first build) | Throwaway-key JWT checks of token validation, OBO, attribution and the auth gate |
| `app/manual_tests/t105_identity_offline_checks.py` | T-105 | `principal_from_udf_context` field parsing; a job run by a different user than the one who reserved it gets `403 FORBIDDEN` and writes nothing |
| `app/manual_tests/t201_offline_checks.py` | T-201 | Checks of BusinessContract validation, round trip, CLI and schema export |
| `app/manual_tests/t202_offline_checks.py` | T-202 | Fake-LLM checks of the compile graph, ambiguity rules, versioning and the /compile endpoint |
| `app/manual_tests/t203_offline_checks.py` | T-203 | Fake-LLM checks of answering, dismissing, locked facts and KPI edits |
| `app/manual_tests/t204_offline_checks.py` | T-204 | Fake-LLM checks of Gate G1, the approval actions, locking, roles and the approvals log |
| `app/manual_tests/t205_offline_checks.py` | T-205 | Checks of `contract` and `artifacts` in the run detail and `GET /me` |
| `app/manual_tests/t301_offline_checks.py` | T-301 | Fake-Lakehouse checks of the harvester, versioning and the harvest/inventory endpoints |
| `app/manual_tests/t302_offline_checks.py` | T-302 | Fake-SQL-endpoint checks of the data profiler and the profile endpoint |
| `app/manual_tests/t303_offline_checks.py` | T-303 | Fake-SQL-and-LLM checks of relationship candidates, evidence, rejections and the endpoints |
| `app/manual_tests/t304_offline_checks.py` | T-304 | Checks of Gate G2, exceptions, Accept of the inventory and data gaps |
| `app/manual_tests/t401_offline_checks.py` | T-401 | Fake-LLM checks of the glossary, source scoring, the conformed policy and `plan/sources` |
| `app/manual_tests/t402_offline_checks.py` | T-402 | Fake-LLM checks of KPI feasibility, data gaps and `plan/feasibility` |
| `app/manual_tests/t403_offline_checks.py` | T-403 | Fake-SQL checks of bridge detection, mapping-table evidence, derivation rules, approvals and `plan/bridges` |
| `app/manual_tests/t404_offline_checks.py` | T-404 | Checks of ModelSpec and ExecutionPlan generation, plan validation, the `plan` endpoints and `cli plan validate` |
| `app/manual_tests/t405_offline_checks.py` | T-405 | Checks of Gate G3, plan edits, Accept and Reject of the ExecutionPlan |
| `app/manual_tests/t501_offline_checks.py` | T-501 | Fake-Fabric checks of the deployment client, the sandbox guard and `cli deploy model` |
| `app/manual_tests/t502_offline_checks.py` | T-502 | Checks of the TMDL generator, `model/generate` and `cli model generate` |
| `app/manual_tests/t503_offline_checks.py` | T-503 | Fake-LLM checks of the DAX pre-check, the `dax_measure` graph and `model/measures` |
| `app/manual_tests/t504_offline_checks.py` | T-504 | Fake-Fabric, Power BI, SQL and LLM checks of the G4 checks, the report and `model/validate` |
| `app/manual_tests/t505_offline_checks.py` | T-505 | Fake-service checks of the repair policy, the `repair` graph, the scope guard and `model/repair` |
| `app/manual_tests/t506_offline_checks.py` | T-506 | Fake-service checks of bridge generation, the notebook, G4.BRIDGE.* and `model/bridges/{id}/generate` |
| `app/manual_tests/t507_offline_checks.py` | T-507 | Checks of the model review data, the role approvals of the SemanticModel and the move to MODEL_APPROVED |
| `app/manual_tests/t601_offline_checks.py` | T-601 | Fake-LLM checks of the report questions, RPLAN.1-RPLAN.7, the planner and `report/plan` |
| `app/manual_tests/t602_offline_checks.py` | T-602 | Fake-Fabric checks of the PBIR generator, `upsert_report` and `report/generate` |
| `app/manual_tests/t603_offline_checks.py` | T-603 | Fake-Fabric and Power BI checks of G5, `report/validate` and `report/repair` |
| `app/manual_tests/t604_offline_checks.py` | T-604 | Checks of the report review data, the file endpoint, the role approvals of the Report and regenerating a visual or page |
| `app/manual_tests/t701_offline_checks.py` | T-701 | Fake-service checks of returning a run, object-level impact, stale marking and its guards, and targeted regeneration |
| `app/manual_tests/t702_offline_checks.py` | T-702 | Fake-Fabric and Power BI checks of the release approval, Gate G6, the lock, smoke tests, completion and rollback |
| `app/manual_tests/t703_offline_checks.py` | T-703 | Checks of the tamper-evident event log, the audit timeline and the trace of a visual |
| `app/manual_tests/t704_offline_checks.py` | T-704 | In-memory-exporter checks of correlation, spans, metrics, model-call cost and the no-content log rule |
| `app/manual_tests/t706_acceptance.py` | T-706 | The integrated offline rehearsal of the vertical slice and the scenario table |
| `app/manual_tests/t705_guard_demo.py` | T-705 | Sends a payload of 10 rows through the shared model client: the guard refuses it |
| `app/manual_tests/t705_offline_checks.py` | T-705 | Fake-vault, fake-model and fake-Fabric checks of secrets, the payload guard, run authorization and the LangSmith block |

## Adding a task

When a task reaches `Awaiting manual test`:

1. Add a section under its milestone heading: `### T-xxx — Title`.
2. Rewrite each acceptance criterion from `.claude/tasks/<milestone>.md` as a numbered step with the **exact command, URL or payload** of the real implementation and its **Expected** result.
3. If a step needs a script, save it as `app/manual_tests/t<id>_<what>.py` (module with a `main()` and a docstring giving the expected result), list it in the table above, and call it with `python -m app.manual_tests.<name>`.
4. Mark each step 💻 (offline) or 🌐 (needs live Azure/Fabric or a browser).
5. Add a clean-up step if the test creates files or edits `.env`.
6. Keep earlier sections accurate: if a later task changes a command or behavior, update the affected steps in the same change.

Order of work for each task: `/implement-task` → run the steps here → `/implement-task done T-xxx` → `/commit-push` → PR → `/cleanup-branch`.
