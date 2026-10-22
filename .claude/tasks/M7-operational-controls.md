# M7 — Operational Controls

**Exit deliverable:** repeatable end-to-end workflow. Shared shapes are in [CONTRACTS.md](CONTRACTS.md); the hosting design is in [fabric-hosting.md](../technical-documentation/fabric-hosting.md).

> **Fabric rework.** T-701 is `Needs Fabric re-test`. T-702 to T-706 were reworked for Fabric and are `Not started`.

---

### T-701 — Impact analysis and stale marking

**Depends on:** T-604 · **Spec:** FR-020, §1.1 principle 8, Business rules 2 and 6, acceptance scenario "Upstream KPI change" · **Fabric hosting:** udf (`POST /runs/{id}/return`, `GET /runs/{id}/impact`); job (`POST /runs/{id}/regenerate-stale`)

> **Fabric re-test.** Logic unchanged from the first build; status `Needs Fabric re-test`. Run the acceptance criteria below through Fabric: an operation listed as `udf` is called from Rayfin once its screen exists, else from the `nexora_api` test pane (`call(method, path, body=…)`); a `job` operation is started from Rayfin or by reserving it (`POST /runs/{id}/jobs`) and running `nexora_stage_runner` ([CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). Its response is the job result. A file named `runs/<id>/…` is in `Files/runs/<id>/…` of `nexora_control` (`python -m app.cli runs pull <id>` copies it locally). CLI steps run locally against the Fabric stores (`RUN_STORE=fabric_sql`, `ARTIFACT_STORE=onelake`).
> - There is still no Rayfin screen for the return and the impact. Use the `nexora_api` test pane.

#### Problem statement
When an approved upstream artifact changes, for example a KPI definition, the artifacts built from it are silently out of date. The spec requires marking dependents stale, blocking progression while they're stale, and regenerating **only** what was affected.

#### Functional requirements
1. A run can be **returned** to an earlier stage (requirement analysis, planning or model). That unlocks the artifact for that stage, with the reason recorded.
2. After the changed artifact is approved again, compute the impact. Compare its old and new versions at **object level** (for example KPI → measure → visuals), using the `based_on` hashes and the dependency map below.
3. Mark the affected artifacts, and the objects within them, as `stale`. A stale artifact can't unlock dependent stages (rule 6).
4. Regenerate or revalidate only the stale objects.

#### API contracts
Dependency map:

| Upstream | Downstream |
|---|---|
| BusinessContract | SourceResolution, Feasibility, ExecutionPlan |
| MetadataInventory | SourceResolution |
| ModelSpec | SemanticModel (TMDL) |
| SemanticModel | ReportPlan |
| ReportPlan | Report (PBIR) |

Object level: a KPI maps to its measure (by `kpi` name), and a measure maps to the visuals that reference it.

| Method and path | Request | Response |
|---|---|---|
| `POST /runs/{id}/return` | `{"to": "REQUIREMENT_ANALYSIS" \| "PLANNING" \| "MODEL", "reason": str}` | `200 RunDetail`. The state becomes `CREATED`, `DATA_PROFILED` or `PLAN_APPROVED` respectively. |
| `GET /runs/{id}/impact` | — | `200 {"changed": [{"artifact", "objects": [..]}], "stale": [{"artifact", "objects": [..], "reason"}]}` |
| `POST /runs/{id}/regenerate-stale` | — | `200 {"regenerated": [...], "validation": [...], "run": RunDetail}` |

#### Constraints
- Returning a run never deletes earlier versions. They stay available for audit (T-703).
- Approvals tied to an old hash are invalidated when the artifact changes.
- Regeneration reuses the generators (T-404, T-502, T-503, T-602) with the T-505 scope guard. The objects outside the impact set stay byte-identical.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| A change with no object-level effect, such as rewording the objective | `stale: []`, and nothing is regenerated. |
| A KPI deleted | Its measure and visuals are marked for removal, and shown for review. They aren't silently dropped. |
| A return requested during `DEPLOYING` | `409 WRONG_STATE`. |
| Deploy attempted with stale artifacts | G6 fails (T-702). |

#### Acceptance criteria
1. **Return and change.** On a `REPORT_APPROVED` run, call `POST /runs/{id}/return` with `{"to": "REQUIREMENT_ANALYSIS", "reason": "Exclude tax"}`. Change the Net Revenue definition to also exclude tax, and re-approve G1.
2. **Impact listed.** Call `GET /runs/{id}/impact`. **Expected:** the Net Revenue measure, its TMDL table file and the visuals that use it are listed as `stale`. Unrelated measures and visuals aren't.
3. **Blocked.** Try to deploy. **Expected:** refused because of stale artifacts.
4. **Targeted regeneration.** Call `POST /runs/{id}/regenerate-stale`. **Expected:** only the stale objects change, which a diff of the model and report version folders confirms. Validation re-runs, and the reviews are required again. This is the "Upstream KPI change" scenario.

---

### T-702 — Promotion deployment, smoke tests and rollback

**Depends on:** T-701 · **Spec:** FR-019, §12 G6, §13 steps 4–7, §8.8, `RunManifest.json` · **Fabric hosting:** job (`POST /runs/{id}/deploy`); udf for the `Release` approval

> **Fabric rework.** Rewritten for Fabric; status `Not started`. G6, the release approval, the hash checks, backup, deploy, smoke tests and rollback logic are kept from the first build. What changes:
> - The deployment **is** the job. The first build's FastAPI background task goes away.
> - The target lock is a row in `dbo.nexora_locks` instead of a lock file.
> - Every Fabric and Power BI call runs as the job user instead of through OBO.
> - The backup goes under `Files/runs/<id>/deploy/`.

#### Problem statement
The approved artifacts are in the sandbox. Moving them to a higher environment must be gated, must use exactly what was approved (verified by hash), must be smoke-tested, and must roll back cleanly on failure. In Fabric this has to run as a job that survives the UI being closed, holds the target lock in the control database, and leaves a correct state if the job itself is cancelled.

#### Functional requirements
1. Gate G6 checks, as before:

   | ID | Check |
   |---|---|
   | G6.1 | The target workspace is authorized for the user (in the job user's workspace list) and isn't the sandbox or the Nexora home workspace |
   | G6.2 | Gates G1–G5 have passed on the current hashes |
   | G6.3 | No stale artifacts |
   | G6.4 | Final release approval recorded (role `release_approver`) |

2. Reserving the job (`POST /runs/{id}/jobs` with `POST /runs/{id}/deploy`) checks the state (`REPORT_APPROVED` or `ROLLED_BACK`). The job then:
   1. runs G6 and the hash checks (`409 HASH_MISMATCH`, read through the artifact store);
   2. takes the target lock (`409 TARGET_BUSY`);
   3. moves the run to `DEPLOYING`;
   4. backs up the target's existing definitions through `getDefinition` into `runs/<id>/deploy/backup-<ts>/`;
   5. deploys the model, then the report, from the approved files;
   6. runs the smoke tests;
   7. finishes as `COMPLETED`, or restores the backup and finishes as `ROLLED_BACK`, or as `NEEDS_INPUT` with a `CRITICAL` reason if the rollback fails;
   8. always releases the lock.
3. **A cancelled or crashed job:** when the run is next read with `active_job.status: failed` while the state is `DEPLOYING`, the dispatcher leaves the state as it is and sets `next_action` to "Resume rollback". A new `POST /runs/{id}/deploy` job, allowed in `DEPLOYING` only in this case, runs the rollback from the saved backup and releases the lock. Nothing is redeployed.
4. Smoke tests are unchanged: `SMOKE.REFRESH`, `SMOKE.MEASURE:<name>` and `SMOKE.REPORT`.

#### API contracts
- `POST /runs/{id}/deploy` with `{"target_workspace_id": GUID}`. The job result is `{"deployment_id", "outcome", "run"}`. Progress is in `GET /runs/{id}` (`state`, `active_job`, `deployment`).
- `RunManifest.deployment` keeps the shape in [CONTRACTS.md#deployment-record](CONTRACTS.md#deployment-record), plus `job_instance_id`.
- `dbo.nexora_locks`: `target_workspace_id` (PK), `run_id`, `deployment_id`, `taken_at`. A lock older than 2 hours is treated as stale, replaced and logged.
- The release approval (`POST /runs/{id}/approvals`, artifact `Release`) is a udf operation, unchanged.
- Only the promotion service builds `FabricItemsClient(allow_any_workspace=True)`, with the job user's token. Every other call stays behind the sandbox guard.

#### Constraints
- One deployment per target workspace at a time, enforced by the lock row with a conditional insert.
- Never deploy into the Nexora home workspace or the sandbox.
- The backup is taken through `getDefinition` and stored in the run folder through the artifact store.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| The target is the sandbox or the home workspace | G6.1 fails; nothing is deployed. |
| The model deploys but the report fails | Both are rolled back. |
| The rollback itself fails | `outcome: rollback_failed`, state `NEEDS_INPUT` with a `CRITICAL` reason. |
| A file differs from the approved hash | `409 HASH_MISMATCH` before anything is deployed. |
| Another deployment to the same target is running | `409 TARGET_BUSY`. |
| The job is cancelled during the deployment | The state stays `DEPLOYING` and `next_action` is "Resume rollback". The resume job restores the backup and releases the lock. |
| The job user can't read the target | G6.1 fails with the reason. |

#### Acceptance criteria
1. **Promote.** Approve the release as `release_approver` in Rayfin, then deploy to a test "higher" workspace. **Expected:** the job runs to the end, the model and report appear in the target, every smoke test passes, the state is `COMPLETED`, and `deployment` records the items, hashes and `job_instance_id`.
2. **Unauthorized target.** Deploy to a workspace you can't access, then to the sandbox. **Expected:** G6.1 fails both times, and nothing is deployed.
3. **Rollback.** Force a smoke-test failure, for example by revoking the target's access to the source Lakehouse. **Expected:** state `ROLLED_BACK`, and the target shows the previous version, or nothing if it was new.
4. **Hash enforced.** Edit a TMDL file of the approved version in `Files/runs/<id>/model/v<n>/` and deploy. **Expected:** `409 HASH_MISMATCH`.
5. **Busy target.** Start two deployments of different runs to the same target. **Expected:** the second fails with `409 TARGET_BUSY`, and the lock row is gone after the first finishes.
6. **Cancelled job.** Cancel a deployment job from the Monitoring hub after the model is deployed. **Expected:** the state stays `DEPLOYING` with "Resume rollback". Start the resume job. **Expected:** the target is restored, the lock is released and the state is `ROLLED_BACK`.

---

### T-703 — Audit trail and traceability view

**Depends on:** T-702 · **Spec:** FR-022, §14 (who approved), FR-021 · **Fabric hosting:** ui (Nexora Run → Audit tab, and the Trace view); `GET /runs/{id}/audit` and `/trace` are udf

> **Fabric rework.** Rewritten for Fabric; status `Not started`. The backend part of this task (services, gates, approvals) is unchanged from the first build and only needs its operations wired through the dispatcher (hosts in [CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). The **UI part is ported** from `rayfin-ui/` (Next.js) to the Rayfin workload `rayfin-workload/` (T-104) as a tab of the **Nexora Run** item editor:
> - The page's display rules move over unchanged (`src/lib/audit.ts` and its tests), and the page calls operations through the workload's `src/lib/api.ts` (`call` for `udf`, `runJob` for `job`, with the job progress shown).
> - Every action that is a `job` disables the run's other actions until it finishes, and shows the job's error envelope if it fails.
> - The acceptance criteria are run in Rayfin in the Fabric portal; "Rayfin" below means the workload.
> - `events.jsonl` is written with `ArtifactStore.append_line` under the run lease (T-101), so the chain stays valid when a UDF and a job both record events.
> - When this task is done, every page of `rayfin-ui/` has been ported: remove `rayfin-ui/` on this branch.

#### Problem statement
A final report must be traceable to the prompt, the approved definitions, the source evidence and the validation results behind it (FR-022). Today that information is spread across many files in the run folder.

#### Functional requirements
1. Build an audit timeline for each run from the history, approvals, artifact versions, gate results, validation reports and deployments.
2. Trace any visual back through: visual → measure → DAX → approved KPI definition → source columns with their profile evidence → original prompt.
3. Show both in Rayfin.
4. Append events to a tamper-evident log, where each event holds the hash of the one before it.

#### API contracts
- `runs/<id>/events.jsonl`, one event per line: `{"seq", "at", "type", "actor", "artifact"?, "version"?, "hash"?, "summary", "prev_event_hash"}`.
- `GET /runs/{id}/audit?offset=0&limit=200` returns `200 {"events": [...], "total", "chain_valid": bool}`.
- `GET /runs/{id}/trace?visual_id=v1` returns `200 {"visual", "measure": {"name", "dax", "model_version"}, "kpi": {"definition", "contract_version", "approved_by", "approved_at"}, "sources": [{"table", "column", "profile_evidence"}], "prompt"}`.
- Workload pages: Nexora Run → **Audit**, and the **Trace** view opened from a report visual (first-build routes `/runs/[id]/audit` and `/runs/[id]/trace/[visualId]`).

#### Constraints
- `events.jsonl` is append-only. `chain_valid` is checked by recomputing the hashes.
- Earlier artifact versions are never deleted, because the trace depends on them.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| A referenced version file is missing | The trace shows an explicit gap ("version 2 missing"). It doesn't fail silently. |
| The event log was edited | `chain_valid: false`, and the first broken `seq` is shown. |
| A long-running run with many events | Paged results. |
| A visual ID that doesn't exist | `404`. |

#### Acceptance criteria
1. **Timeline.** Open `/runs/[id]/audit` for a `COMPLETED` run. **Expected:** every stage, with who approved it and when, and the artifact hashes.
2. **Trace.** Open the trace for `v1`. **Expected:** you reach the original prompt through the measure, its DAX, the approved KPI and the source columns with their evidence, without leaving Rayfin.
3. **Tamper detection.** Edit one line of a copy of `events.jsonl`, pointing the run at it in a test, and load the audit. **Expected:** `chain_valid: false`, with the broken `seq`.

---

### T-704 — Observability

**Depends on:** T-702 · **Spec:** NFR-007, §7 (Observability layer) · **Fabric hosting:** library code in every UDF and job; sink Eventhouse `nexora_telemetry`

> **Fabric rework.** Rewritten for Fabric; status `Not started`. The first build's spans, metrics, JSON logs, redaction and pricing are kept. What changes:
> - The correlation context is bound by the dispatcher and the job runner, because there is no HTTP middleware.
> - The default sink is the home workspace's Eventhouse.
> - The Fabric Monitoring hub links each job to its `run_id`.

#### Problem statement
There is no way to tell how long each stage took, how many retries happened, or what model calls cost for a run (NFR-007). In Fabric, work is spread over UDF invocations and notebook jobs, so the run id must connect them all, and the telemetry should stay inside Fabric by default.

#### Functional requirements
1. Use `run_id` as the correlation ID on every log line, span and outbound HTTP call. The dispatcher binds it for every UDF call that names a run. `run_job` binds it, and the `job_id` and `job_instance_id`, for a job.
2. Record a span per operation (`stage.<name>`, named as in the first build from the operation path) with its duration, outcome and attempt. Record a child span per graph node (`run_graph`).
3. Record the metrics: stage duration, errors, retries, validation passed, LLM input and output tokens, LLM cost.
4. Export with `OTEL_EXPORTER`:
   - `eventhouse` (default in Fabric): to KQL database `nexora_telemetry`, tables `nexora_logs`, `nexora_spans` and `nexora_metrics`, through its ingestion endpoint or an Eventstream custom endpoint;
   - `azuremonitor`: as in the first build;
   - `console`: local development and notebook output.
5. The job's notebook exit value includes `run_id` and `operation`, so the Monitoring hub entry can be matched to the run.

#### API contracts
- Settings in the Variable library: `OTEL_EXPORTER` (`eventhouse` | `azuremonitor` | `console`), `NEXORA_EVENTHOUSE_URI`, `NEXORA_EVENTHOUSE_DATABASE`, and `APPLICATIONINSIGHTS_CONNECTION_STRING` for `azuremonitor` (from Key Vault).
- Span names, attributes and metric names: as in the first build (`stage.<name>`, `stage.<name>.node.<node>`; `run_id`, `stage`, `attempt`, `outcome`; `nexora.stage.duration`, `nexora.errors`, `nexora.retries`, `nexora.validation.passed`, `nexora.llm.tokens.input`, `nexora.llm.tokens.output`, `nexora.llm.cost`), plus `job_id` on job spans.
- `config/model_pricing.yaml` in `Files/config/`.
- Logs are JSON lines: `{"ts", "level", "run_id", "stage", "job_id"?, "msg", …}`.

#### Constraints
- Never log prompts, model responses, tokens or secrets in full; log their length and a hash.
- Telemetry failure must not break a run.
- Telemetry data stays in the tenant: Eventhouse by default, Azure Monitor only if chosen.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| The exporter or Eventhouse is unreachable | The run continues; one warning per process (per job or UDF instance). |
| Pricing missing for a deployment | `cost` not recorded; a warning names the deployment. |
| Work outside a run (`/health`, smoke) | No `run_id` attribute; nothing fails. |
| A UDF instance is reused across calls | The context is cleared after each call, so no `run_id` leaks into the next one. |

#### Acceptance criteria
1. **Correlated.** Complete a compile and a harvest in Rayfin. In `nexora_telemetry`, run `nexora_spans | where run_id == "<id>" | project name, duration_ms, outcome`. **Expected:** the UDF calls and both job stages, each with a duration.
2. **Costs and retries.** Run a model repair on a run. **Expected:** `nexora_metrics` has `nexora.retries` for it, and `nexora.llm.tokens.*` and `nexora.llm.cost` for its model calls.
3. **No prompt leakage.** Search `nexora_logs` for a phrase from the prompt. **Expected:** not found; only the length and hash appear.
4. **Monitoring hub.** Open the compile job in the Monitoring hub. **Expected:** its exit value shows the `run_id` and the operation.
5. **Console in dev.** Locally with `OTEL_EXPORTER=console`, run a CLI-driven stage script. **Expected:** spans with the `run_id` on stdout.

---

### T-705 — Security hardening

**Depends on:** T-704 · **Spec:** NFR-001, NFR-002, NFR-008, §14 · **Fabric hosting:** secrets in jobs, run authorization in the dispatcher (UDF and job), `job:check_access`

> **Fabric rework.** Rewritten for Fabric; status `Not started`. The first build's payload guard, LangSmith block and 401-retry are kept. What changes:
> - Secrets come from Key Vault through `notebookutils` in jobs, and the API client secret is gone with OBO.
> - Run authorization moves into the dispatcher, with the workspace-access decision cached in the control database, because a UDF can't read the user's workspaces.

#### Problem statement
Secrets must not live in `.env`, a notebook or the Variable library. Only authorized users may see or act on a run. Only metadata and profile summaries may reach the model. §14 requires Key Vault, scoping and data minimization. In Fabric the model is called only from jobs, and a UDF can't ask Fabric which workspaces the user can see, so the authorization decision has to be made where the user's token exists and then reused.

#### Functional requirements
1. **Secrets.** In a job, `app/security/secrets.py` reads the model key (`azure-openai-api-key`) with `notebookutils.credentials.getSecret(KEY_VAULT_URL, name)`, as the job user or the workspace identity, whichever Key Vault grants (record which in `fabric-hosting.md`). Locally, `DefaultAzureCredential` is used, as before. `.env` and the Variable library hold no secret. `entra-api-client-secret` is no longer needed.
2. **Run authorization, in the dispatcher, for every operation on `/runs/{id}/…`:**
   - The run's creator (oid or UPN) may always act.
   - Otherwise the dispatcher looks up `dbo.nexora_run_access` for `(tenant, scope workspace, user oid)` checked within the last 5 minutes.
   - With no fresh row, a UDF answers `403 ACCESS_CHECK_REQUIRED`, and the UI runs `job:check_access`. The job lists the user's workspaces as the user, writes the row (`allowed` true or false) and returns. The UI then retries.
   - A job checks access itself before doing anything.
   - A run with no `created_by` (CLI) stays open to any signed-in user. An owned run with no scope is the owner's alone.
3. `GET /runs` lists only the runs the user may open: the ones they created, plus the ones whose workspace has a fresh `allowed` row.
4. Caches are scoped by tenant and workspace.
5. **LLM payload guard:** unchanged. Every model payload is checked in the shared client (`check_payload`). Row-like data is rejected with `422 PAYLOAD_REJECTED`, and the size and shape of every payload are logged.
6. **No external tracing:** `LANGSMITH_TRACING` or `LANGCHAIN_TRACING_V2` set to true fails every job (and `app.llm.client` import) with `CONFIG_ERROR`.

#### API contracts
- Setting `KEY_VAULT_URL` in the Variable library. Secret `azure-openai-api-key`.
- `dbo.nexora_run_access`: `tenant_id`, `workspace_id`, `user_oid`, `allowed`, `checked_at`, with PK `(tenant_id, workspace_id, user_oid)`.
- `POST /runs/{id}/check_access` (job): result `{"allowed": bool, "checked_at"}`.
- `app/security/llm_guard.py:check_payload(payload: dict) -> None` raises `PayloadRejected(reason)`, as before.
- If T-105 found that a UDF can get a user-delegated Fabric token, the UDF makes the check itself and `ACCESS_CHECK_REQUIRED` is never returned. Record the choice in `fabric-hosting.md`.

#### Constraints
- No secret value in the Variable library, `fabric/`, notebook output, logs or errors.
- A job that can't reach Key Vault fails at the start with `CONFIG_ERROR` naming `KEY_VAULT_URL` and the vault host, never a value.
- The guard runs in the shared model client, so no code path can skip it.
- A Fabric list that can't be read is a refusal (`allowed` is not written).

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| Key Vault unreachable or access denied in a job | The job fails with `CONFIG_ERROR` naming `KEY_VAULT_URL`; the secret is never printed. |
| A secret is rotated | On a model `401` the secret is re-read and the call retried once. |
| A user is removed from a workspace | They lose access to its runs within 5 minutes, when the row expires. |
| No fresh access row, non-creator, UDF call | `403 ACCESS_CHECK_REQUIRED`; the UI runs `check_access` and retries once. |
| `check_access` says not allowed | `403 FORBIDDEN` on the retry. |
| Payload rejected by the guard | The job fails with `422 PAYLOAD_REJECTED`; nothing is sent; the reason is logged. |

#### Acceptance criteria
1. **Secrets from Key Vault.** Make sure `nexora_settings` and `.env` have no `AZURE_OPENAI_API_KEY`, set `KEY_VAULT_URL`, and compile a run in Rayfin. **Expected:** it works.
2. **Run isolation.** As user B, with no access to user A's workspace, open user A's run (its item, or `call("GET", "/runs/<A's run>")`). **Expected:** the access check runs, then `403 FORBIDDEN`. The run doesn't appear in B's run list.
3. **Access granted.** Give user B access to the workspace and open the run again. **Expected:** after the access check, the run opens.
4. **Guard blocks rows.** In a notebook, call the shared model client with a payload containing a list of 10 row dictionaries. **Expected:** `PayloadRejected`, no request sent, and the reason in the logs.
5. **Payload log.** Review the payload log for a full run in `nexora_logs`. **Expected:** only allowed shapes, with their sizes.
6. **LangSmith blocked.** Set `LANGSMITH_TRACING=true` in `nexora_settings` and start a compile. **Expected:** the job fails with `CONFIG_ERROR` naming the setting. Remove it afterwards.

---

### T-706 — End-to-end vertical slice acceptance

**Depends on:** T-705 · **Spec:** [Definition of Done](../spec/acceptance-scenarios.md#definition-of-done-for-the-initial-vertical-slice), [acceptance scenarios](../spec/acceptance-scenarios.md), [review checklist](../spec/acceptance-scenarios.md#recommended-review-checklist) · **Fabric hosting:** the whole system, in the Fabric portal

> **Fabric rework.** Status `Not started`. The first build's evidence ([T-706-evidence.md](T-706-evidence.md)) was collected on the local hosting and doesn't count for Fabric. Collect it again in `T-706-fabric-evidence.md`.

#### Problem statement
Each task was tested on its own. The vertical slice is done only when one real Sales prompt goes from Rayfin **in the Fabric portal** all the way to a deployed, traceable report, and every acceptance scenario passes on the integrated system as hosted on Fabric: workload, `nexora_api`, stage jobs, control store and Eventhouse.

#### Functional requirements
1. Run one complete Sales run in Rayfin in the Fabric portal, from prompt to `COMPLETED`, with no CLI, test pane or local process involved.
2. Re-run all seven acceptance scenarios on the integrated system.
3. Walk through the review checklist with the business, BI and security reviewers.
4. Fix only the defects found. **No new features** in this task.
5. Confirm that the home workspace can be rebuilt from the repo: `python -m app.tools.deploy_home --env test --create Nexora-test`, then run the Simple Sales KPI scenario there.

#### API contracts
No new contracts. Record the evidence in `.claude/tasks/T-706-fabric-evidence.md`: run ids, job instance ids, screenshots or their paths, Eventhouse queries, and file paths in `nexora_control`.

#### Constraints
- Use the sandbox, and a test "higher" workspace for promotion. Never production.
- Each defect fix goes into its own commit, referencing the scenario it fixes.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| A scenario fails | Record it, fix it on this branch, and re-run **every** scenario. |
| A fix needs a new feature | Stop. Add a new task to the plan instead. |
| A platform limit blocks a scenario (UDF timeout, payload size) | Move the operation to `job` per T-102's rule and re-run every scenario. |

#### Acceptance criteria
1. **Definition of Done.** Every item in the [Definition of Done](../spec/acceptance-scenarios.md#definition-of-done-for-the-initial-vertical-slice) is ticked, with evidence from Fabric.
2. **Scenarios.** Every row passes on the integrated system in Fabric:

   | Scenario | First proven in | Result | Run ID |
   |---|---|---|---|
   | Simple Sales KPI | T-706 | | |
   | Ambiguous Revenue | T-205 | | |
   | Invalid relationship | T-504 | | |
   | Many-to-many bridge | T-506 | | |
   | Unsupported bridge | T-403 | | |
   | Report binding failure | T-603 | | |
   | Upstream KPI change | T-701 | | |

3. **Review checklist.** Every [review checklist](../spec/acceptance-scenarios.md#recommended-review-checklist) question is answered "yes" by the responsible reviewer.
4. **Rebuildable.** FR-5 passes in `Nexora-test`.
5. **Status tracker.** The [status tracker](../technical-documentation/roadmap-risks-decisions.md#project-status-tracking) is updated, with every workstream `Done`.
6. **M7 exit.** **Expected:** the workflow is repeatable end to end on Fabric.
