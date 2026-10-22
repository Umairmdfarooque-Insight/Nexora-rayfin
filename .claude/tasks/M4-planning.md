# M4 — Planning

**Exit deliverable:** approved execution and model plan. Shared shapes are in [CONTRACTS.md](CONTRACTS.md); the hosting design is in [fabric-hosting.md](../technical-documentation/fabric-hosting.md).

> **Fabric rework.** T-401 to T-404 are `Needs Fabric re-test`. T-405 (UI) was reworked and is `Not started`.

---

### T-401 — Entity-to-source mapping and conformed-table resolver

**Depends on:** T-304 · **Spec:** FR-009, §8.4, Business rule 5 · **Fabric hosting:** job (`POST /runs/{id}/plan/sources`); udf for `GET /runs/{id}/plan/sources`

> **Fabric re-test.** Logic unchanged from the first build; status `Needs Fabric re-test`. Run the acceptance criteria below through Fabric: an operation listed as `udf` is called from Rayfin once its screen exists, else from the `nexora_api` test pane (`call(method, path, body=…)`); a `job` operation is started from Rayfin or by reserving it (`POST /runs/{id}/jobs`) and running `nexora_stage_runner` ([CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). Its response is the job result. A file named `runs/<id>/…` is in `Files/runs/<id>/…` of `nexora_control` (`python -m app.cli runs pull <id>` copies it locally). CLI steps run locally against the Fabric stores (`RUN_STORE=fabric_sql`, `ARTIFACT_STORE=onelake`).
> - `config/glossary.yaml` and `config/conformed_sources.yaml` are read from `Files/config/` on every call.

#### Problem statement
`requirement_data_matcher.py` uses a hardcoded `ALIASES` dict, and `semantic_mapper.py` returns LLM suggestions with no scoring. Neither explains why a table was chosen or rejected. FR-009 requires preferring approved conformed tables and recording the rationale for every selected and rejected source.

#### Functional requirements
1. For each contract entity and KPI, list candidate tables from:
   - the glossary-driven deterministic matcher
   - LLM suggestions, which are discarded if they aren't in the inventory
2. Score each candidate deterministically. Select the best table per entity, and list the rejected ones with reasons.
3. Prefer conformed tables. A non-conformed table can be selected only if it has an approved exception from T-304.
4. Map each KPI's required fields to concrete columns.
5. Save the result as an artifact.

#### API contracts
- Config `config/glossary.yaml`, which replaces `ALIASES`:
  ```yaml
  terms:
    product: [product, products, item, sku]
    customer: [customer, customers, client]
    sales: [sales, salesorder, salesorderdetail, order line]
  ```
- Score from 0 to 1: `0.4·name_match + 0.3·column_coverage + 0.2·conformed + 0.1·relationship_confidence`. Ties are broken by conformed first, then alphabetical table name.
- `POST /runs/{id}/plan/sources` returns `200 SourceResolution`. It's allowed in `DATA_PROFILED`.

`runs/<id>/SourceResolution.json`:
```json
{
  "schema_version": 1, "run_id": "…", "version": 1,
  "based_on": { "BusinessContract": "sha256:…", "MetadataInventory": "sha256:…" },
  "entities": [
    {
      "entity": "Product",
      "selected": { "table": "saleslt.SalesLT_Product", "score": 0.93, "conformed": true, "exception_id": null, "rationale": "Glossary match 'product'; conformed; key ProductID unique" },
      "rejected": [ { "table": "nexora_test.dim_product_dupes", "score": 0.41, "reason": "Not conformed; ProductID not unique" } ]
    }
  ],
  "kpi_fields": [
    { "kpi": "Net Revenue", "fields": [ { "need": "line amount", "table": "saleslt.SalesLT_SalesOrderDetail", "column": "LineTotal", "role": "measure" } ] }
  ]
}
```

`role` is `measure` | `filter` | `time` | `dimension`.

#### Constraints
- The LLM sees only names, types, the contract and the glossary. It never sees data values.
- Scoring is deterministic: the same inputs always give the same selection.
- No hardcoded table names in code. They live only in config files.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| An entity has no candidate | Selected is `null`, and the reason is recorded. T-402 turns it into a data gap. |
| The LLM proposes a table not in the inventory | Discarded and logged, and never selected. |
| Two tables with equal scores | Tie-break rule. Both are shown in `rejected` / `selected` with their scores. |
| Glossary file malformed | `500 CONFIG_ERROR`, naming the file and line. |
| Wrong state | `409 WRONG_STATE`. |

#### Acceptance criteria
1. **Correct selection.** On the Net Revenue run, call `POST /runs/{id}/plan/sources`. **Expected:** Sales maps to `SalesLT_SalesOrderDetail` (with `SalesOrderHeader` for the order status), Product maps to `SalesLT_Product`, and every selection has a rationale.
2. **Rejections explained.** On a run scoped to both `saleslt` and `nexora_test`, check Product. **Expected:** `dim_product_dupes` is in `rejected`, with a reason that mentions conformed status or uniqueness.
3. **Conformed policy.** Remove `SalesLT_Product` from `conformed_sources.yaml` without an exception. **Expected:** it isn't selected, or the response flags that it requires an exception.
4. **Glossary without code.** Add `item` to `product` in `glossary.yaml` and use "items" in a new prompt. **Expected:** it maps to Product with no code change.
5. **Deterministic.** Call it twice. **Expected:** identical `SourceResolution.json` apart from timestamps.

---

### T-402 — KPI feasibility and Data Gap

**Depends on:** T-401 · **Spec:** Business rule 8, §2.4 (no inventing KPIs), G1/G3 feasibility flags · **Fabric hosting:** job (`POST /runs/{id}/plan/feasibility`)

> **Fabric re-test.** Logic unchanged from the first build; status `Needs Fabric re-test`. Run the acceptance criteria below through Fabric: an operation listed as `udf` is called from Rayfin once its screen exists, else from the `nexora_api` test pane (`call(method, path, body=…)`); a `job` operation is started from Rayfin or by reserving it (`POST /runs/{id}/jobs`) and running `nexora_stage_runner` ([CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). Its response is the job result. A file named `runs/<id>/…` is in `Files/runs/<id>/…` of `nexora_control` (`python -m app.cli runs pull <id>` copies it locally). CLI steps run locally against the Fabric stores (`RUN_STORE=fabric_sql`, `ARTIFACT_STORE=onelake`).
> - To leave `NEEDS_INPUT` after a data gap, use `python -m app.cli runs transition RUN_ID DATA_PROFILED` locally against the Fabric run store.

#### Problem statement
Nothing stops the system from producing a substitute calculation when the data can't support a KPI. Business rule 8 requires returning **Not Feasible / Data Gap** in that case, with no fabricated calculation.

#### Functional requirements
1. For each approved KPI, determine the fields needed by its formula intent, filters, time logic and exclusions. The LLM extracts them in a structured form; the deterministic check then confirms each one maps to a column in `SourceResolution`.
2. A KPI is `feasible` only if **every** needed field maps to a column. Otherwise it is `data_gap`, listing what's missing.
3. Any data gap moves the run to `NEEDS_INPUT`, with these resolution options:
   - narrow the scope (drop the KPI, which returns to requirement analysis)
   - provide a source
   - raise a data-engineering task

#### API contracts
- `POST /runs/{id}/plan/feasibility` returns `200 {"kpis": [Feasibility], "run": RunDetail}`.

`Feasibility` is saved to `runs/<id>/Feasibility.json` and referenced by the ExecutionPlan:
```json
{ "kpi": "Customer Retention Rate", "status": "data_gap",
  "needed": [ { "need": "customer activity per period", "mapped": null } ],
  "missing": [ { "need": "customer activity per period", "reason": "No table with customer activity history in scope" } ],
  "options": ["drop_kpi", "provide_source", "data_engineering_task"] }
```

`status` is `feasible` | `data_gap`.

#### Constraints
- The deterministic check has the final say. If the LLM says a field exists but it isn't in the inventory, the KPI is a data gap.
- No proxy or partial measure is ever proposed for a `data_gap` KPI.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| KPI partially supported, for example one exclusion field missing | `data_gap`, not partly feasible. |
| Time logic needs a date column that isn't there | `data_gap`, with the missing date named. |
| Every KPI is a data gap | `NEEDS_INPUT`, and the plan can't be built. |
| The LLM extraction returns invalid output twice | `502 LLM_INVALID_OUTPUT`. The state is unchanged. |

#### Acceptance criteria
1. **Unsupported KPI stopped.** Approve a contract for "Customer Retention Rate by quarter", which `saleslt` can't support, then resolve sources and run feasibility. **Expected:** `data_gap` with the missing inputs listed, state `NEEDS_INPUT`, and no DAX or proxy measure anywhere in the run folder.
2. **Supported KPI passes.** Run feasibility on the Net Revenue run. **Expected:** `feasible`, with every needed field mapped to a column.
3. **Deterministic override.** Edit `SourceResolution.json` by hand to remove the `LineTotal` mapping, then re-run feasibility. **Expected:** Net Revenue becomes `data_gap`.

---

### T-403 — Bridge-table detection and proposal

**Depends on:** T-401 · **Spec:** FR-008, §15 Bridge-Table Design, Business rule 4 · **Fabric hosting:** job (`POST /runs/{id}/plan/bridges`); udf for `POST /runs/{id}/approvals` with `BridgeProposal:<id>`

> **Fabric re-test.** Logic unchanged from the first build; status `Needs Fabric re-test`. Run the acceptance criteria below through Fabric: an operation listed as `udf` is called from Rayfin once its screen exists, else from the `nexora_api` test pane (`call(method, path, body=…)`); a `job` operation is started from Rayfin or by reserving it (`POST /runs/{id}/jobs`) and running `nexora_stage_runner` ([CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). Its response is the job result. A file named `runs/<id>/…` is in `Files/runs/<id>/…` of `nexora_control` (`python -m app.cli runs pull <id>` copies it locally). CLI steps run locally against the Fabric stores (`RUN_STORE=fabric_sql`, `ARTIFACT_STORE=onelake`).
> - `config/derivation_rules.yaml` is read from `Files/config/`.

#### Problem statement
Many-to-many relationships cause wrong filtering or double counting. The system must detect them, and propose a bridge **only** when a mapping table or an approved derivation rule exists. It must never guess a mapping.

#### Functional requirements
1. Detect many-to-many needs from two sources:
   - relationship evidence with `many_to_many` between entities the contract needs
   - entity pairs with no direct relationship, but a table holding keys to both
2. Classify each case into one of the §15 situations, and produce a **proposal**. Nothing is generated in this task.
3. Validate an existing mapping table: duplicate pairs and key coverage on each side.
4. With no mapping table and no approved rule, move the run to `NEEDS_INPUT` and request the mapping rules.

#### API contracts
- Config `config/derivation_rules.yaml` lists the approved rules, for example:
  ```yaml
  rules:
    - id: DR1
      entities: [Product, Region]
      description: Product sold in region if any order line ships there
      approved_by: user@contoso.com
  ```
- `POST /runs/{id}/plan/bridges` returns `200 [BridgeProposal]`.
- Approval goes through `POST /runs/{id}/approvals` with `artifact: "BridgeProposal:<id>"`, by roles `bi_engineer` and `business_owner`.

`BridgeProposal`:
```json
{ "id": "B1", "entities": ["Product", "Promotion"],
  "situation": "existing_table",
  "table": "nexora_test.bridge_product_promotion", "derivation_rule": null,
  "evidence": { "duplicate_pairs": 0, "coverage_a_pct": 42.0, "coverage_b_pct": 100.0 },
  "issues": [], "status": "proposed" }
```

- `situation` is `existing_table` | `derivation_rule` | `needs_rules`.
- `status` is `proposed` | `approved` | `rejected`.

#### Constraints
- Approving the design doesn't guarantee mapping logic exists (§15). A `needs_rules` proposal can't be approved.
- Evidence queries are read-only and aggregate-only.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| The mapping table has duplicate pairs | `issues: ["duplicate pairs: N"]`. It can be approved only if a deduplication rule is approved; otherwise T-506 fails validation. |
| Several possible mapping tables | The one with the highest coverage is proposed, and the others are listed in `issues`. |
| A many-to-many that no KPI needs | Ignored, and logged. |
| A `needs_rules` proposal is approved | `409`: "mapping rules required". |

#### Acceptance criteria
1. **Existing mapping proposed.** Use a contract needing sales by Promotion and Product, against `nexora_test`. **Expected:** proposal `B1` with `situation: existing_table`, table `bridge_product_promotion`, `duplicate_pairs: 0`, and coverage figures.
2. **No guess.** Use a contract needing Product by Campaign. **Expected:** `situation: needs_rules`, state `NEEDS_INPUT`, a message asking for mapping rules, and no table proposed.
3. **Can't approve without rules.** Try to approve the `needs_rules` proposal. **Expected:** `409`.

---

### T-404 — ExecutionPlan and ModelSpec generation

**Depends on:** T-402, T-403 · **Spec:** FR-010, §8.4, `ExecutionPlan.yaml` and `ModelSpec.yaml` (§10), §2.3 (Direct Lake / Import) · **Fabric hosting:** udf (`POST /runs/{id}/plan` and `GET /runs/{id}/plan`; deterministic, control files only)

> **Fabric re-test.** Logic unchanged from the first build; status `Needs Fabric re-test`. Run the acceptance criteria below through Fabric: an operation listed as `udf` is called from Rayfin once its screen exists, else from the `nexora_api` test pane (`call(method, path, body=…)`); a `job` operation is started from Rayfin or by reserving it (`POST /runs/{id}/jobs`) and running `nexora_stage_runner` ([CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). Its response is the job result. A file named `runs/<id>/…` is in `Files/runs/<id>/…` of `nexora_control` (`python -m app.cli runs pull <id>` copies it locally). CLI steps run locally against the Fabric stores (`RUN_STORE=fabric_sql`, `ARTIFACT_STORE=onelake`).
> - AC-2's hand edit of `ModelSpec.yaml` is made in `Files/runs/<id>/` of `nexora_control` (or pull, edit and upload it). `python -m app.cli plan validate runs/<id>` reads it through the artifact store.
> - If T-102 finds the plan generation can exceed the UDF timeout on a realistic inventory, move it to `job` in CONTRACTS.md.

#### Problem statement
There's no reviewable plan between the selected sources and the generated model. FR-010 requires defining, **before** generation: the fact grain, dimensions, relationships, storage mode, measures, hierarchies and RLS.

#### Functional requirements
1. Generate `ExecutionPlan.yaml`: the selected sources, the stages and their gates, the target, the required approvals, the bridges and any data-engineering actions.
2. Generate `ModelSpec.yaml`: the model's tables and their roles, relationships (each with a reference to its evidence), storage mode, measures (formula intent only, with no DAX yet), hierarchies, formatting, display folders and RLS roles.
3. Validate both with a schema check, reference checks and policy rules.
4. Include only feasible KPIs as measures, and add RLS roles only from the contract's `security` section.

#### API contracts
- `POST /runs/{id}/plan` returns `200 {"execution_plan": …, "model_spec": …, "validation": GateResult-like}`. It's allowed in `DATA_PROFILED`.
- `GET /runs/{id}/plan` returns the same files.
- CLI: `python -m app.cli plan validate runs/<id>`.

`ModelSpec.yaml`:
```yaml
schema_version: 1
run_id: …
version: 1
based_on: { BusinessContract: "sha256:…", MetadataInventory: "sha256:…", SourceResolution: "sha256:…" }
model_name: Nexora_6f1c
storage_mode: DirectLake        # DirectLake | Import
tables:
  - name: Sales
    source: { schema: saleslt, table: SalesLT_SalesOrderDetail }
    role: fact                  # fact | dimension | bridge | date
    grain: order line
    columns:
      - { name: LineTotal, source_column: LineTotal, data_type: decimal, format_string: "#,0.00", display_folder: null, is_hidden: false }
    hierarchies: []
  - name: Date
    source: { schema: nexora_test, table: dim_date }    # physical date table (T-004); Direct Lake can't use calculated tables
    role: date
    hierarchies:
      - { name: Calendar, levels: [ { name: Year, column: Year }, { name: Quarter, column: Quarter }, { name: Month, column: MonthName } ] }
relationships:
  - { name: Sales_Product, from: { table: Sales, column: ProductID }, to: { table: Product, column: ProductID }, cardinality: many_to_one, cross_filter: single, evidence_ref: R1 }
measures:
  - { name: Net Revenue, table: Sales, kpi: Net Revenue, formula_intent: "…", dax: null, format_string: "$#,0", display_folder: Revenue, description: "Approved: SUM(LineTotal) excluding cancelled orders" }
roles: []                        # from contract.security only
```

`ExecutionPlan.yaml`:
```yaml
schema_version: 1
run_id: …
version: 1
based_on: { ModelSpec: "sha256:…" }
sources: [ { table: saleslt.SalesLT_SalesOrderDetail, role: fact, conformed: true, exception_id: null } ]
stages:
  - { name: semantic_model, gate: G4, depends_on: [plan] }
  - { name: report, gate: G5, depends_on: [semantic_model] }
  - { name: deployment, gate: G6, depends_on: [report] }
target: { workspace_id: "<SANDBOX_WORKSPACE_ID>", kind: sandbox }
approvals_required:
  - { gate: G3, roles: [bi_engineer] }
  - { gate: G4, roles: [bi_engineer, business_owner] }
bridges: [B1]
data_engineering_actions: []
feasibility_ref: Feasibility.json
```

#### Constraints
- Generation is deterministic, using the resolution, the evidence and the contract. The LLM may suggest descriptions only.
- Every relationship must reference an `evidence_ref` with confidence `high` or `medium`, or carry an explicit override with a rationale (Business rule 7).
- Direct Lake is the default for Lakehouse Delta sources. Direct Lake models can't use calculated tables.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| No date table in the sources but time logic is needed | Add a `data_engineering_actions` entry to create a date table in the Lakehouse. Don't invent a calculated table. |
| A relationship with `low` confidence and no override | Validation fails, naming the relationship. |
| A reference to a non-existent table or column | Validation fails, naming the path. |
| A contract with no security requirements | `roles: []`. Never inferred (§14). |
| Every KPI is a data gap | `409`: "no feasible KPIs". |

#### Acceptance criteria
1. **Plan generated.** On the Net Revenue run, call `POST /runs/{id}/plan`. **Expected:** both YAML files exist. `ModelSpec` has the Sales fact at grain "order line", relationships to Product, Customer and Date (each with an `evidence_ref`), `storage_mode: DirectLake`, and a Net Revenue measure with `dax: null`.
2. **Bad reference caught.** Edit a relationship in `ModelSpec.yaml` so it references `Product.NoSuchColumn`, then run `python -m app.cli plan validate runs/<id>`. **Expected:** it fails, naming the path.
3. **No inferred security.** On a contract without `security`, check `roles`. **Expected:** `[]`.
4. **Missing date handled.** On a run where the scope has no date table, check `ExecutionPlan.data_engineering_actions`. **Expected:** an action to create a date table, and no calculated table in `ModelSpec`.

---

### T-405 — Gate G3 and plan review UI

**Depends on:** T-404 · **Spec:** §12 G3, FR-010, FR-018, NFR-004 · **Fabric hosting:** ui (Nexora Run → Plan tab); `/plan/sources`, `/plan/feasibility` and `/plan/bridges` are jobs; G3, `POST`/`PATCH /plan` and approvals are udf

> **Fabric rework.** Rewritten for Fabric; status `Not started`. The backend part of this task (services, gates, approvals) is unchanged from the first build and only needs its operations wired through the dispatcher (hosts in [CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). The **UI part is ported** from `rayfin-ui/` (Next.js) to the Rayfin workload `rayfin-workload/` (T-104) as a tab of the **Nexora Run** item editor:
> - The page's display rules move over unchanged (`src/lib/plan.ts` and its tests), and the page calls operations through the workload's `src/lib/api.ts` (`call` for `udf`, `runJob` for `job`, with the job progress shown).
> - Every action that is a `job` disables the run's other actions until it finishes, and shows the job's error envelope if it fails.
> - The acceptance criteria are run in Rayfin in the Fabric portal; "Rayfin" below means the workload.

#### Problem statement
A BI engineer must approve the selected tables, joins, storage mode and scope before any model is generated. That needs automated G3 checks, editable plans, and a review screen that shows the rationale and evidence.

#### Functional requirements
1. G3 runs automated checks on the ExecutionPlan and ModelSpec.
2. The plan can be edited with a small set of operations. Every edit re-runs validation and G3.
3. When a `bi_engineer` Accepts and G3 passes, the run moves to `PLAN_APPROVED`.
4. The Rayfin **Plan review** screen shows:
   - selected and rejected tables, with their rationale
   - relationships, with their evidence
   - measures and their formula intent
   - storage mode, RLS, data-engineering actions and the approval actions

#### API contracts
G3 checks:

| ID | Check |
|---|---|
| G3.1 | Fact grain defined |
| G3.2 | Every source is conformed, or has an exception |
| G3.3 | Every relationship has evidence or an override |
| G3.4 | Stage and gate dependencies are complete |
| G3.5 | No open `data_engineering_actions` that block generation |
| G3.6 | Every measure maps to a feasible KPI |

| Method and path | Request | Response |
|---|---|---|
| `POST /runs/{id}/gates/G3/check` | — | `200 GateResult` |
| `PATCH /runs/{id}/plan` | `{"expected_hash": str, "ops": [{"op": "remove_relationship", "name": "Sales_Product"}, {"op": "set_storage_mode", "value": "Import"}, {"op": "override_relationship", "name": "…", "rationale": "…"}, {"op": "remove_measure", "name": "…"}]}` | `200 {"model_spec", "execution_plan", "gate_result"}` |
| `POST /runs/{id}/approvals` | `{"artifact": "ExecutionPlan", "action": "ACCEPT", …}` | as in T-204 |

Workload page: Nexora Run → **Plan** (first-build route `/runs/[id]/plan`).

#### Constraints
- Edits create a new ModelSpec and ExecutionPlan version. Earlier versions are kept.
- Only `bi_engineer` can Accept or Reject the plan.
- Rejecting moves the run back to `DATA_PROFILED`, with the comment recorded.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| Removing a relationship that a measure needs | G3.6, or validation, fails: "Net Revenue needs Sales_Product". |
| An unknown `op` | `422`, listing the valid ops. |
| `expected_hash` doesn't match | `409 STALE_VERSION`. |
| Blocking data-engineering action open | G3.5 fails. The action is shown, with an owner field. |

#### Acceptance criteria
1. **Rationale visible.** Open `/runs/[id]/plan`. **Expected:** for each table, why it was chosen, and the rejected alternatives with their reasons.
2. **Edit re-validates.** Remove the `Sales_Date` relationship through Edit. **Expected:** G3 re-runs and fails, naming the measure that depends on the date.
3. **Approve.** Restore the relationship (or reload the previous version) and Accept as a `bi_engineer`. **Expected:** state `PLAN_APPROVED`, with an approval record holding the ExecutionPlan hash.
4. **Role enforced.** Accept as a user without `bi_engineer`. **Expected:** `403`.
5. **M4 exit.** **Expected:** an approved execution and model plan exists and is reviewable in Rayfin.
