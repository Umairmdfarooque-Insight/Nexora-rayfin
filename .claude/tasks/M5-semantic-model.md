# M5 — Semantic Model

**Exit deliverable:** approved semantic model. Shared shapes are in [CONTRACTS.md](CONTRACTS.md); the hosting design is in [fabric-hosting.md](../technical-documentation/fabric-hosting.md).

> **Fabric rework.** T-501 to T-506 are `Needs Fabric re-test`. T-507 (UI) was reworked and is `Not started`.

> The Fabric and Power BI REST details below (item-definition parts, `executeQueries`, refresh) are written from Microsoft's public APIs. **Check them against the current Microsoft documentation** when starting each task, and update the task if anything differs.

---

### T-501 — Sandbox deployment client

**Depends on:** T-105 · **Spec:** FR-019, §8.8, §13 steps 1 and 7, NFR-003 (idempotency) · **Fabric hosting:** library used by jobs (no operation of its own); the CLI `deploy model` locally

> **Fabric re-test.** Logic unchanged from the first build; status `Needs Fabric re-test`. Run the acceptance criteria below through Fabric: an operation listed as `udf` is called from Rayfin once its screen exists, else from the `nexora_api` test pane (`call(method, path, body=…)`); a `job` operation is started from Rayfin or by reserving it (`POST /runs/{id}/jobs`) and running `nexora_stage_runner` ([CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). Its response is the job result. A file named `runs/<id>/…` is in `Files/runs/<id>/…` of `nexora_control` (`python -m app.cli runs pull <id>` copies it locally). CLI steps run locally against the Fabric stores (`RUN_STORE=fabric_sql`, `ARTIFACT_STORE=onelake`).
> - `SANDBOX_WORKSPACE_ID` comes from the Variable library in Fabric, `.env` locally.
> - The token provider is T-002's: the job user's token in Fabric, the interactive credential for the CLI. There is no OBO token.

#### Problem statement
Generated semantic models must be created and updated in Fabric reliably. Fabric's create and update calls are asynchronous long-running operations, and repeated deployments must not create duplicates. Before promotion, nothing may be deployed anywhere except the sandbox.

#### Functional requirements
1. Create a semantic model from a TMDL definition, or update it if one with the same display name already exists in the workspace.
2. Poll long-running operations until they succeed or fail, honouring `Retry-After`.
3. Allow only the configured sandbox workspace as a target. Promotion in T-702 uses a separate, gated path.
4. Provide a CLI for deploying a TMDL folder by hand.

#### API contracts
- Setting: `SANDBOX_WORKSPACE_ID`, added to `.env.example`.
- `app/deploy/fabric_items.py`, `class FabricItemsClient`:
  - `upsert_semantic_model(workspace_id: str, display_name: str, tmdl_dir: Path) -> {"item_id": str, "operation": "created" | "updated"}`
  - `get_item_by_name(workspace_id, display_name, item_type) -> dict | None`
  - `wait_for_operation(location_url: str, timeout_s: int = 600) -> dict`
- Upstream calls:
  - `POST /v1/workspaces/{ws}/semanticModels` with `{"displayName", "definition": {"parts": [{"path", "payload" (base64), "payloadType": "InlineBase64"}]}}`
  - `POST /v1/workspaces/{ws}/semanticModels/{id}/updateDefinition`
  - Definition parts: `definition.pbism`, plus every file under `definition/`, such as `database.tmdl`, `model.tmdl` and `tables/*.tmdl`.
- CLI: `python -m app.cli deploy model TMDL_DIR --name NAME` prints the item ID and the operation.

#### Constraints
- The target guard is enforced inside the client: `workspace_id != SANDBOX_WORKSPACE_ID` raises `TargetNotAllowed` unless the call comes from the promotion service (T-702).
- Uses the user's token for `FABRIC_SCOPE` from the T-002 provider (the job user in Fabric; the first build used OBO from T-105).
- Idempotent: running the same deployment twice leaves exactly one item.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| `202` with `Location` | Poll until `Succeeded` or `Failed`, using `Retry-After` between polls. |
| Operation `Failed` | `UpstreamError`, with Fabric's error code and message and the request ID. |
| `429` | Wait for the `Retry-After` delay, retrying up to 5 times. |
| An item with the same name exists but is a different type | `409`: "name used by a <type> item". |
| Invalid TMDL definition rejected by Fabric | Fabric's error is surfaced unchanged in `details`. |
| Polling exceeds the timeout | `UPSTREAM_TIMEOUT`. The item state is reported as unknown. |
| Target isn't the sandbox | `403 TARGET_NOT_ALLOWED`. |

#### Acceptance criteria
1. **Create.** Hand-write a minimal TMDL folder: one table over `SalesLT_Product` and a measure `Product Count = COUNTROWS('Product')`. Run `python -m app.cli deploy model <dir> --name Nexora_Smoke`. **Expected:** `operation: created`, and the model appears in the sandbox workspace and opens in the web model view.
2. **Update, not duplicate.** Rename the measure and deploy again. **Expected:** `operation: updated`. The workspace still has one `Nexora_Smoke`, showing the new measure name.
3. **Guard.** Deploy with a non-sandbox workspace ID. **Expected:** refused with `TARGET_NOT_ALLOWED`, and nothing is created there.
4. **Failure surfaced.** Introduce a TMDL syntax error and deploy. **Expected:** a readable Fabric error with the request ID.

---

### T-502 — TMDL generator

**Depends on:** T-405, T-501 · **Spec:** FR-011, §8.5, §9 (TMDL row: deterministic serialization) · **Fabric hosting:** udf (`POST /runs/{id}/model/generate`; deterministic, control files only); the CLI `model generate` locally

> **Fabric re-test.** Logic unchanged from the first build; status `Needs Fabric re-test`. Run the acceptance criteria below through Fabric: an operation listed as `udf` is called from Rayfin once its screen exists, else from the `nexora_api` test pane (`call(method, path, body=…)`); a `job` operation is started from Rayfin or by reserving it (`POST /runs/{id}/jobs`) and running `nexora_stage_runner` ([CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). Its response is the job result. A file named `runs/<id>/…` is in `Files/runs/<id>/…` of `nexora_control` (`python -m app.cli runs pull <id>` copies it locally). CLI steps run locally against the Fabric stores (`RUN_STORE=fabric_sql`, `ARTIFACT_STORE=onelake`).
> - The TMDL writer writes to a staging folder (a temporary folder in a UDF, the default-Lakehouse mount in a job) and the folder is copied to `Files/runs/<id>/model/v<n>/` through `ArtifactStore.copy_tree`, so a version is still never modified once written.
> - AC-2's `git diff --no-index` runs on folders copied with `python -m app.cli runs pull <id>`.

#### Problem statement
The approved ModelSpec has to become a TMDL project Fabric accepts. That conversion must be deterministic and must not depend on the LLM, so the model structure is exactly what was approved and is reproducible (NFR-009).

#### Functional requirements
1. Serialize `ModelSpec.yaml` into a TMDL folder. It covers:
   - the database and model
   - one file per table, with its columns, format strings, display folders, hidden flags and hierarchies
   - relationships
   - the Direct Lake expression and partitions, or the Import partitions
2. Version the output per run as `runs/<id>/model/v<n>/`, and never overwrite an existing version.
3. Add an entry point that starts model generation: `PLAN_APPROVED → MODEL_GENERATING`.
4. Leave out measures whose `dax` is `null`. T-503 fills them in.

#### API contracts
- `app/model/tmdl_generator.py`: `generate_tmdl(spec: ModelSpec, out_dir: Path) -> list[Path]`.
- Output layout:
  ```
  runs/<id>/model/v1/
    definition.pbism
    definition/database.tmdl
    definition/model.tmdl
    definition/relationships.tmdl
    definition/expressions.tmdl        # Direct Lake source expression
    definition/tables/<Table>.tmdl
  ```
- `POST /runs/{id}/model/generate` returns `200 {"version": int, "files": [str], "run": RunDetail}`. It's allowed in `PLAN_APPROVED`, and in `MODEL_FAILED` when called by repair.
- CLI: `python -m app.cli model generate runs/<id>` writes the next version.

#### Constraints
- Byte-identical output for the same ModelSpec:
  - sorted objects
  - LF line endings
  - tab indentation
  - `lineageTag` values from `uuid5(namespace, run_id + object path)`
- Object names that need quoting in TMDL are quoted, for example `'Sales Order'`.
- No LLM calls in this module.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| ModelSpec fails validation | Refused, with the validation errors. Nothing is written. |
| A name with quotes or special characters | Escaped correctly, and round-trips. |
| The version folder already exists | A new `v<n+1>` is written. The existing folder is never modified. |
| Import storage mode | Import partitions are written instead of Direct Lake, as the spec says. |
| Generate in the wrong state | `409 WRONG_STATE`. |

#### Acceptance criteria
1. **Generated.** Call `POST /runs/{id}/model/generate` on a `PLAN_APPROVED` run. **Expected:** `runs/<id>/model/v1/` with one table file per ModelSpec table, and state `MODEL_GENERATING`.
2. **Deterministic.** Run `python -m app.cli model generate runs/<id>`, then `git diff --no-index runs/<id>/model/v1 runs/<id>/model/v2`. **Expected:** no difference.
3. **Deployable.** Deploy `v1` with `python -m app.cli deploy model runs/<id>/model/v1 --name Nexora_<run>`. **Expected:** in the web model view, the tables, the relationships (with cardinality and direction as in the spec) and the Calendar hierarchy are visible.
4. **No LLM.** Disconnect the network LLM setting (for example, use an invalid endpoint) and generate again. **Expected:** it still succeeds.

---

### T-503 — DAX measure generation

**Depends on:** T-502 · **Spec:** FR-011, §9 (KPI logic row), Business rules 1–2 · **Fabric hosting:** job (`POST /runs/{id}/model/measures`)

> **Fabric re-test.** Logic unchanged from the first build; status `Needs Fabric re-test`. Run the acceptance criteria below through Fabric: an operation listed as `udf` is called from Rayfin once its screen exists, else from the `nexora_api` test pane (`call(method, path, body=…)`); a `job` operation is started from Rayfin or by reserving it (`POST /runs/{id}/jobs`) and running `nexora_stage_runner` ([CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). Its response is the job result. A file named `runs/<id>/…` is in `Files/runs/<id>/…` of `nexora_control` (`python -m app.cli runs pull <id>` copies it locally). CLI steps run locally against the Fabric stores (`RUN_STORE=fabric_sql`, `ARTIFACT_STORE=onelake`).

#### Problem statement
Each approved KPI needs a DAX measure that implements **exactly** the approved definition. The LLM is good at drafting DAX, but its output can reference objects that don't exist or drift from the definition. It must be treated as a candidate and checked before use.

#### Functional requirements
1. For each ModelSpec measure, the LLM proposes DAX from:
   - the approved KPI's formula intent, grain, filters, time logic and exclusions
   - the ModelSpec object names
2. A deterministic pre-check extracts every `Table[Column]` and `[Measure]` reference from the DAX, and rejects the proposal if any reference isn't in the ModelSpec.
3. Accepted DAX is written into the ModelSpec's `dax`, and a new TMDL version is generated with the measures included.
4. Each measure's description quotes the approved definition.

#### API contracts
- `app/model/dax_generator.py`: `propose_measure_dax(kpi: Kpi, spec: ModelSpec) -> DaxProposal`.
- `DaxProposal` (structured LLM output): `{"measure_name": str, "dax": str, "referenced_objects": [{"table"?, "column"?, "measure"?}], "explanation": str}`.
- `POST /runs/{id}/model/measures` returns `200 {"measures": [{"name", "dax", "status": "accepted" | "rejected", "reason"?}], "version": int}`.
- `app/graphs/dax_measure.py`: `build_dax_measure_graph(llm=…)`, a stage graph ([CONTRACTS.md#stage-graphs](CONTRACTS.md#stage-graphs)). Nodes: `propose_dax → precheck_references → (retry propose_dax with the errors)`. `propose_measure_dax` invokes it once per measure.

#### Constraints
- The LLM receives only the KPI definition and the ModelSpec names and types. It never receives data values.
- At most 2 attempts per measure at this stage, enforced by the graph's attempt counter. Further repair belongs to T-505.
- Each measure has its own graph invocation, so one rejected measure doesn't stop the others.
- Every reference must be fully qualified (`'Table'[Column]`).
- Use `DIVIDE()` for ratios. No hardcoded literal values unless the KPI definition states them.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| DAX references an unknown column | The proposal is rejected, and one retry is made with the error included. It's then marked `rejected` with the reason. |
| `referenced_objects` doesn't match the references in the DAX | Rejected. Extraction from the DAX text is the source of truth. |
| KPI uses time intelligence, such as YoY, with no table marked as a date table | Rejected with "date table required", and no DAX is invented. |
| Column name that exists in several tables | Must be fully qualified, or it's rejected. |
| LLM timeout or invalid output | The measure is marked `rejected`, and the other measures continue. |

#### Acceptance criteria
1. **Measures added.** Call `POST /runs/{id}/model/measures` on the Net Revenue run. **Expected:** Net Revenue is `accepted`. Its DAX references only existing objects and applies the cancelled-orders exclusion, and its description quotes the approved definition.
2. **Deployed and computes.** Deploy the new version. In the web model view, run `EVALUATE ROW("NR", [Net Revenue])`. **Expected:** a number.
3. **Matches SQL.** Hand-write the equivalent SQL in the SQL endpoint, a SUM of LineTotal excluding cancelled orders. **Expected:** the same value. T-504 automates this.
4. **Bad reference rejected.** Temporarily rename a ModelSpec column so the LLM's likely reference fails, then regenerate. **Expected:** `rejected`, with a reason naming the missing reference.

---

### T-504 — Model validators and Gate G4

**Depends on:** T-503 · **Spec:** FR-012, §12 G4, `ValidationReport.json` (§10), §8.7 · **Fabric hosting:** job (`POST /runs/{id}/model/validate`)

> **Fabric re-test.** Logic unchanged from the first build; status `Needs Fabric re-test`. Run the acceptance criteria below through Fabric: an operation listed as `udf` is called from Rayfin once its screen exists, else from the `nexora_api` test pane (`call(method, path, body=…)`); a `job` operation is started from Rayfin or by reserving it (`POST /runs/{id}/jobs`) and running `nexora_stage_runner` ([CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). Its response is the job result. A file named `runs/<id>/…` is in `Files/runs/<id>/…` of `nexora_control` (`python -m app.cli runs pull <id>` copies it locally). CLI steps run locally against the Fabric stores (`RUN_STORE=fabric_sql`, `ARTIFACT_STORE=onelake`).
> - Fabric item deployment, refresh and `executeQueries` run as the job user (T-002 `POWERBI_SCOPE` / `FABRIC_SCOPE`). Check that the notebook's Power BI token allows the dataset refresh and `executeQueries` calls (the first build needed `Dataset.ReadWrite.All` on its OBO app), and record the result in the platform-limits table of `fabric-hosting.md`.
> - `config/validation.yaml` and `config/relationship_thresholds.yaml` are read from `Files/config/`.

#### Problem statement
A model that deploys can still return wrong numbers or break relationships. FR-012 requires checking references, relationships, DAX execution, RLS and reconciliation against independent figures. A failed model must not progress to report generation.

#### Functional requirements
1. Deploy the current model version to the sandbox, then refresh it (Direct Lake framing).
2. Run these checks and record each one in `ValidationReport.json`:

   | ID | Check |
   |---|---|
   | G4.REF | Every measure, relationship and hierarchy references existing objects |
   | G4.REL | Re-run uniqueness and orphan checks on each relationship's sources |
   | G4.DAX | Every measure executes without error |
   | G4.RECON | Each measure's total, and one sliced value, match the independent figure within tolerance |
   | G4.RLS | Each role exists, its filter expression is valid, and a query with an effective identity returns filtered data |

3. Reconciliation is checked against two references:
   - the contract's `expected_examples`, where the business provided values
   - an independent SQL query for the same KPI, run on the SQL endpoint and shown for review
4. If any check fails, the run moves to `MODEL_FAILED`. If all pass, the run stays in `MODEL_GENERATING`, awaiting review in T-507.

#### API contracts
- `POST /runs/{id}/model/validate` returns `200 {"report": ValidationReport run entry, "run": RunDetail}`. The shape is in [CONTRACTS.md#validation-report](CONTRACTS.md#validation-report).
- Upstream calls:
  - Power BI `POST https://api.powerbi.com/v1.0/myorg/datasets/{id}/refreshes`, polled until done.
  - Power BI `POST https://api.powerbi.com/v1.0/myorg/datasets/{id}/executeQueries`, with body `{"queries": [{"query": "<DAX>"}], "serializerSettings": {"includeNulls": true}, "impersonatedUserName"?: "<upn>"}`. Impersonation is used for the RLS tests.
- Config `config/validation.yaml`: `reconciliation: { relative_tolerance: 0.0001, absolute_tolerance: 0.01 }`.

#### Constraints
- Read-only against source data. Writes go only to the sandbox model.
- The reconciliation SQL is generated separately from the DAX, so it can serve as an independent check. Both queries and both values are stored in the evidence.
- No report-stage endpoint accepts a run in `MODEL_FAILED`.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| Refresh fails | `G4.DAX` fails with the refresh error. The other checks are skipped and marked `skipped`. |
| Measure returns blank | `warning` if no expected value exists; `fail` if a non-blank value is expected. |
| No `expected_examples` | Reconciliation uses the SQL reference only. The evidence notes "no business example provided". |
| Model has no RLS roles | `G4.RLS` is `pass`, with "no roles defined". |
| `executeQueries` throttled (`429`) | Retry with backoff, then `UPSTREAM_ERROR`. |
| Value differs by more than the tolerance | `G4.RECON` fails, showing both values and the difference. |

#### Acceptance criteria
1. **Passing model.** Validate the T-503 model. **Expected:** every check passes, and `G4.RECON` evidence shows the DAX and SQL values side by side.
2. **Broken measure fails.** Edit the DAX in `ModelSpec` to use `'Sales'[LineTotalX]`, regenerate and validate. **Expected:** `G4.REF` and `G4.DAX` fail with the error text, and the state is `MODEL_FAILED`.
3. **Invalid relationship.** On a run whose plan uses `nexora_test.dim_product_dupes` as a dimension, under an override, validate. **Expected:** `G4.REL` fails, with duplicate-key evidence. This is the "Invalid relationship" scenario.
4. **Report blocked.** While the run is `MODEL_FAILED`, call the report-plan endpoint (T-601). **Expected:** `409 WRONG_STATE`.
5. **Tolerance.** Set `expected_examples` to a deliberately wrong value and validate. **Expected:** `G4.RECON` fails, showing both values.

---

### T-505 — Validation and Repair service

**Depends on:** T-504 · **Spec:** FR-016, FR-017, §8.7, §9 (Regeneration row), NFR-003 · **Fabric hosting:** job (`POST /runs/{id}/model/repair`)

> **Fabric re-test.** Logic unchanged from the first build; status `Needs Fabric re-test`. Run the acceptance criteria below through Fabric: an operation listed as `udf` is called from Rayfin once its screen exists, else from the `nexora_api` test pane (`call(method, path, body=…)`); a `job` operation is started from Rayfin or by reserving it (`POST /runs/{id}/jobs`) and running `nexora_stage_runner` ([CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). Its response is the job result. A file named `runs/<id>/…` is in `Files/runs/<id>/…` of `nexora_control` (`python -m app.cli runs pull <id>` copies it locally). CLI steps run locally against the Fabric stores (`RUN_STORE=fabric_sql`, `ARTIFACT_STORE=onelake`).
> - AC-1's `git diff --no-index` runs on folders copied with `python -m app.cli runs pull <id>`. `config/repair_policy.yaml` is read from `Files/config/`.

#### Problem statement
When validation fails, rebuilding everything is wasteful and can break parts that worked. Retrying without limits wastes cost. FR-016 and FR-017 require the smallest possible repair, a bounded number of retries, detection of no progress, and escalation to the user.

#### Functional requirements
1. Normalize each failed check into an error with a **fingerprint**.
2. Classify each error by policy as one of:
   - `deterministic` (fixed by code)
   - `llm` (regenerate only that object)
   - `user_input` (escalate)
3. Repair only the failing objects into a new version. Every other file stays byte-identical, and a scope guard enforces this.
4. Re-validate after each attempt.
5. Stop at the retry limit, or when there's no progress (the same fingerprint after an attempt). Then move the run to `NEEDS_INPUT`, with a message for the user.
6. The service is generic, so T-603 can reuse it for reports.

#### API contracts
- `app/repair/service.py`: `repair(run_id, artifact: "SemanticModel" | "Report") -> RepairResult`.
- `app/graphs/repair.py`: `build_repair_graph(llm=…)`, a stage graph ([CONTRACTS.md#stage-graphs](CONTRACTS.md#stage-graphs)):
  - Nodes: `classify → deterministic_fix → llm_fix → scope_guard → validate → check_progress`.
  - `check_progress` loops back to `classify`, or ends with outcome `passed` | `limit_reached` | `no_progress` | `user_input_only`.
  - Graph state: `{run_id, artifact, version, errors, attempts, seen_fingerprints, outcome}`. `RepairResult` is built from it.
- Run transitions are made by `repair()`, never by the graph:

  | Outcome | Transitions (model; the report uses the `REPORT_*` states) |
  |---|---|
  | `user_input_only` before any attempt | `MODEL_FAILED → NEEDS_INPUT` |
  | `passed` | `MODEL_FAILED → MODEL_GENERATING` (awaiting review in T-507) |
  | `limit_reached` or `no_progress` | `MODEL_FAILED → NEEDS_INPUT`, with `resume_state: MODEL_FAILED` |
- `RepairResult`: `{"attempts": [{"attempt", "fixed": [fingerprint], "remaining": [fingerprint], "classification": {...}, "version"}], "final_state": RunState, "message"?: str}`.
- `POST /runs/{id}/model/repair` returns `200 RepairResult`. It's allowed in `MODEL_FAILED`.
- Config `config/repair_policy.yaml`:
  ```yaml
  max_attempts: 3
  policy:
    FORMAT_STRING_INVALID: deterministic
    DAX_SYNTAX: llm
    DAX_REFERENCE: llm
    RECON_MISMATCH: llm
    SOURCE_COLUMN_MISSING: user_input
    REL_DUPLICATE_KEYS: user_input
  default: user_input
  ```
- The fingerprint is `sha256(code + object + normalized message)`, with numbers and IDs stripped from the message.

#### Constraints
- **Scope guard:** after an attempt, diff the new version against the previous one. If any file outside the failing objects changed, discard the attempt and count it as failed.
- Deterministic fixes run before LLM fixes.
- Every attempt is appended to `ValidationReport.json`, with its retry count. The `validate` node does this.
- The retry limit and the no-progress check are conditional edges on `check_progress`, not LLM decisions. `run_graph` gets a `recursion_limit` sized to `max_attempts`.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| A fix introduces a different error | That's progress (a new fingerprint), but the attempt still counts toward the limit. |
| The LLM edits another measure | Discarded by the scope guard and counted as an attempt. |
| Only `user_input` errors | Escalate immediately with no attempts. State `NEEDS_INPUT`. |
| Limit reached with errors left | `NEEDS_INPUT`, with `resume_state: MODEL_FAILED` and a summary of what remains. |
| Repair called when nothing failed | `409`: "nothing to repair". |

#### Acceptance criteria
1. **Targeted repair.** Inject a typo into one measure's DAX, validate (it fails), then call `POST /runs/{id}/model/repair`. **Expected:** one attempt, and the validation passes. Running `git diff --no-index runs/<id>/model/v<n> runs/<id>/model/v<n+1>` shows **only** that measure's table file changed. This is the Definition of Done item on intentional model failure.
2. **Escalation.** Make a KPI depend on a source column that no longer exists (a `user_input` error). **Expected:** no LLM attempts, state `NEEDS_INPUT`, and a clear message naming the column.
3. **Retry limit.** Force a failure the LLM can't fix, such as a reconciliation tolerance of 0 with a rounding difference. **Expected:** at most 3 attempts, or an earlier no-progress stop, then `NEEDS_INPUT`.
4. **Audit.** Open `ValidationReport.json`. **Expected:** each attempt, with its number, its fingerprints and the version written.
5. **Graph boundary.** After AC-3, run `python -m app.cli runs show <id>`. **Expected:** the repair call added only the transitions in the outcome table, not one per attempt. The individual attempts appear only in `ValidationReport.json`.

---

### T-506 — Bridge-table generation

**Depends on:** T-505, T-403 · **Spec:** FR-008, §15, Business rule 4, acceptance scenario "Many-to-many bridge" · **Fabric hosting:** job (`POST /runs/{id}/model/bridges/{bridge_id}/generate`)

> **Fabric re-test.** Logic unchanged from the first build; status `Needs Fabric re-test`. Run the acceptance criteria below through Fabric: an operation listed as `udf` is called from Rayfin once its screen exists, else from the `nexora_api` test pane (`call(method, path, body=…)`); a `job` operation is started from Rayfin or by reserving it (`POST /runs/{id}/jobs`) and running `nexora_stage_runner` ([CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). Its response is the job result. A file named `runs/<id>/…` is in `Files/runs/<id>/…` of `nexora_control` (`python -m app.cli runs pull <id>` copies it locally). CLI steps run locally against the Fabric stores (`RUN_STORE=fabric_sql`, `ARTIFACT_STORE=onelake`).
> - The bridge notebook is still deployed to and run in the **sandbox** workspace, from inside the stage job, as the job user. It never runs in the home workspace.

#### Problem statement
An approved bridge proposal must be turned into real model objects, with two one-to-many relationships and correct filter directions. It must then be validated so that totals aren't double counted.

#### Functional requirements
1. For an **approved** proposal:
   - **`existing_table`:** add the mapping table to the ModelSpec with `role: bridge`, plus a one-to-many relationship from each dimension to it. Filter directions come from the approved proposal.
   - **`derivation_rule`:** generate a Spark notebook in the sandbox that materializes the bridge table from the approved rule. Run it, validate the result, then reference it as for an existing table.
2. Validate:
   - unique pairs
   - orphan keys on each side, within the thresholds
   - **no double counting:** with no filters, the grand total of each affected measure equals the total without the bridge
3. Regenerate TMDL with the change (through T-502), and validate it with G4.

#### API contracts
- `POST /runs/{id}/model/bridges/{bridge_id}/generate` returns `200 {"version", "validation": ValidationReport run entry}`.
- Generated notebook (`derivation_rule` only): `runs/<id>/bridges/<bridge_id>.ipynb`, deployed to the sandbox workspace.
- New checks:

  | ID | Check |
  |---|---|
  | G4.BRIDGE.UNIQUE | No duplicate pairs |
  | G4.BRIDGE.ORPHANS | Orphan keys within thresholds |
  | G4.BRIDGE.TOTALS | Grand totals are unchanged |

#### Constraints
- Only approved proposals are allowed. A `needs_rules` proposal can never be generated.
- Deduplicate only under an approved deterministic rule (§15). Otherwise fail.
- The notebook writes only to the sandbox Lakehouse.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| Proposal not approved | `409`: "bridge not approved". |
| Bridge has duplicate pairs and no dedup rule | `G4.BRIDGE.UNIQUE` fails, and the duplicates are shown. |
| Bridge rows reference unknown keys | `G4.BRIDGE.ORPHANS` gives a warning or fails, according to the thresholds. |
| Derivation notebook run fails | `UPSTREAM_ERROR`, with the notebook run ID. The ModelSpec is unchanged. |
| Bridge table is empty | Fails with "empty bridge". |

#### Acceptance criteria
1. **Bridge generated.** Approve proposal `B1` (Product × Promotion) from T-403 and generate it. **Expected:** `bridge_product_promotion` is in the model with two one-to-many relationships, and every `G4.BRIDGE.*` check passes.
2. **No double counting.** Run `EVALUATE ROW("All", [Net Revenue])` on the model version before the bridge and on the version with it, and compare both with the SQL total. **Expected:** all three values match.
3. **Per-promotion values.** Run a DAX query for Net Revenue by PromotionName, and compare it with a hand-written SQL join through the bridge. **Expected:** the values match.
4. **Duplicates fail.** Add a duplicate pair to the bridge with the fixture notebook, then re-validate. **Expected:** `G4.BRIDGE.UNIQUE` fails, showing the pair.

---

### T-507 — Model review and approval UI

**Depends on:** T-506 · **Spec:** §12 G4 (human decision), §2.2 approval scopes, FR-018, Business rule 2 · **Fabric hosting:** ui (Nexora Run → Model tab); generate, review and approvals are udf; measures, validate, repair and bridges are jobs

> **Fabric rework.** Rewritten for Fabric; status `Not started`. The backend part of this task (services, gates, approvals) is unchanged from the first build and only needs its operations wired through the dispatcher (hosts in [CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). The **UI part is ported** from `rayfin-ui/` (Next.js) to the Rayfin workload `rayfin-workload/` (T-104) as a tab of the **Nexora Run** item editor:
> - The page's display rules move over unchanged (`src/lib/model.ts` and its tests), and the page calls operations through the workload's `src/lib/api.ts` (`call` for `udf`, `runJob` for `job`, with the job progress shown).
> - Every action that is a `job` disables the run's other actions until it finishes, and shows the job's error envelope if it fails.
> - The acceptance criteria are run in Rayfin in the Fabric portal; "Rayfin" below means the workload.

#### Problem statement
Validation can't confirm business meaning on its own. The model must be reviewed by people within their own scopes. The BI engineer checks technical correctness, the business owner checks that each measure implements the approved definition **without redefining it**, and the security reviewer checks RLS.

#### Functional requirements
1. The review data combines the tables, relationships, each measure's DAX next to its approved KPI definition, the reconciliation values, the RLS roles and the latest ValidationReport.
2. Required approvals:
   - `bi_engineer` always
   - `business_owner` always
   - `security_reviewer` when the model has RLS roles
3. When every required approval is recorded on the **current model hash** and G4 passes, the run moves to `MODEL_APPROVED`.
4. A change to the model's hash invalidates the earlier approvals, which must be given again.
5. Rayfin has a **Model review** screen with the approval actions and a per-role status.

#### API contracts
- `GET /runs/{id}/model/review` returns `200 {"model_hash", "tables", "relationships", "measures": [{"name", "dax", "kpi_definition", "reconciliation": {"dax_value", "sql_value", "expected", "status"}}], "roles", "validation_report", "approvals_required": [role], "approvals_done": [ApprovalRecord]}`.
- `POST /runs/{id}/approvals`, with `{"artifact": "SemanticModel", "action": …, "expected_hash": model_hash}`.
- Workload page: Nexora Run → **Model** (first-build route `/runs/[id]/model`).

#### Constraints
- A business owner's Reject that says the definition itself is wrong must return the run to requirement analysis (T-701). It can't change the KPI here (rule 2).
- The approval records carry the role used.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| Approve while G4 isn't passing | `409 GATE_FAILED`. |
| The model changes after a partial approval | The earlier approvals are shown as "invalidated: model changed", and are required again. |
| The same user holds several roles | Each role must be approved explicitly, as a separate action. |
| A role not required for this model | Not shown, and not counted. |

#### Acceptance criteria
1. **Side-by-side review.** Open `/runs/[id]/model`. **Expected:** the Net Revenue DAX next to "SUM(LineTotal) excluding cancelled orders", with the DAX, SQL and expected values.
2. **Partial approval.** Approve as `bi_engineer` only. **Expected:** the state is still `MODEL_GENERATING`, and `business_owner` is shown as pending.
3. **Invalidation.** Trigger a repair that changes the model, then reopen the review. **Expected:** the `bi_engineer` approval is marked invalidated.
4. **Complete.** Give every required approval on the current hash. **Expected:** `MODEL_APPROVED`, with one attributable record per role.
5. **M5 exit.** **Expected:** an approved semantic model exists in the sandbox.
