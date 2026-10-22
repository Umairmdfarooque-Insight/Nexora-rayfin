# M6 — Report

**Exit deliverable:** approved report artifact. Shared shapes are in [CONTRACTS.md](CONTRACTS.md); the hosting design is in [fabric-hosting.md](../technical-documentation/fabric-hosting.md).

> **Fabric rework.** T-601 to T-603 are `Needs Fabric re-test`. T-604 (UI) was reworked and is `Not started`.

> PBIR is an evolving format with published JSON schemas, and report export requires Fabric or Premium capacity. **Check the current Microsoft documentation** when starting each task, and update the task if anything differs.

---

### T-601 — Report planner

**Depends on:** T-507 · **Spec:** FR-013, §8.6, `ReportPlan.json` (§10), §9 (Report row) · **Fabric hosting:** job (`POST /runs/{id}/report/plan`); udf for `GET /runs/{id}/report/plan`

> **Fabric re-test.** Logic unchanged from the first build; status `Needs Fabric re-test`. Run the acceptance criteria below through Fabric: an operation listed as `udf` is called from Rayfin once its screen exists, else from the `nexora_api` test pane (`call(method, path, body=…)`); a `job` operation is started from Rayfin or by reserving it (`POST /runs/{id}/jobs`) and running `nexora_stage_runner` ([CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). Its response is the job result. A file named `runs/<id>/…` is in `Files/runs/<id>/…` of `nexora_control` (`python -m app.cli runs pull <id>` copies it locally). CLI steps run locally against the Fabric stores (`RUN_STORE=fabric_sql`, `ARTIFACT_STORE=onelake`).

#### Problem statement
A report must answer the business questions in the approved contract, using only approved model objects. Without a reviewable plan, generated reports can bind to fields that don't exist, or miss the questions the business asked.

#### Functional requirements
1. Derive the business questions from the contract (its objective, KPIs, dimensions and desired outputs), and the intended users from the run.
2. The LLM proposes pages, visuals, filters, drill paths and interactions as structured output.
3. Deterministic validation checks:
   - the plan schema
   - **every referenced field, measure or hierarchy exists in the approved ModelSpec**
   - every question maps to at least one visual
4. If a question can't be answered with the approved measures, the plan flags it. The planner never invents a measure.

#### API contracts
- `POST /runs/{id}/report/plan` returns `200 {"plan": ReportPlan, "validation": {...}}`. It's allowed in `MODEL_APPROVED`.

`runs/<id>/ReportPlan.json`:
```json
{
  "schema_version": 1, "run_id": "…", "version": 1,
  "based_on": { "SemanticModel": "sha256:…", "BusinessContract": "sha256:…" },
  "audience": ["Sales leadership"],
  "questions": [ { "id": "Q-R1", "text": "How does Net Revenue trend by month in 2024?", "source": "contract.kpis[Net Revenue]" } ],
  "pages": [
    {
      "name": "Overview", "purpose": "Monthly revenue trend",
      "visuals": [
        { "id": "v1", "type": "lineChart", "title": "Net Revenue by Month",
          "question_ids": ["Q-R1"],
          "fields": { "category": [ { "hierarchy": "Calendar", "table": "Date" } ], "values": [ { "measure": "Net Revenue" } ] },
          "filters": [], "drill": { "hierarchy": "Calendar" } }
      ],
      "filters": [ { "table": "Date", "column": "Year", "values": [2024] } ]
    }
  ],
  "interactions": [ { "from_visual": "v2", "to_visual": "v1", "type": "filter" } ],
  "unanswered_questions": []
}
```

- Allowed visual `type`s: `card`, `lineChart`, `clusteredBarChart`, `clusteredColumnChart`, `tableEx`, `pivotTable`, `slicer`.
- Interaction `type` is `filter` | `highlight` | `none`.

#### Constraints
- The LLM receives only the contract and the ModelSpec names, types and descriptions. It never receives data values.
- At most 8 visuals per page.
- Visual IDs are unique and stable, as `v<n>`.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| A question that no approved measure answers | Listed in `unanswered_questions`. The run moves to `NEEDS_INPUT` with "return to model or drop question". |
| A reference to a non-existent object | Validation fails, naming the visual and the field. |
| Duplicate visual IDs | Validation fails. |
| More than 8 visuals needed | Split across pages. |
| An unsupported visual type | Validation fails, listing the allowed types. |
| Wrong state | `409 WRONG_STATE`. |

#### Acceptance criteria
1. **Questions covered.** Call `POST /runs/{id}/report/plan` on a `MODEL_APPROVED` run. **Expected:** a visual answers "Net Revenue by month", and every contract question maps to a visual through `question_ids`.
2. **Bad reference caught.** Edit a visual in `ReportPlan.json` to use `{"measure": "Net Revenue X"}` and re-validate. **Expected:** a failure naming `v1` and `Net Revenue X`.
3. **No invented measures.** Add a contract question needing a KPI the model doesn't have (by returning the run to requirement analysis, or using a fixture contract). **Expected:** it appears in `unanswered_questions`, with no new measure.

---

### T-602 — PBIR generator and sandbox publish

**Depends on:** T-601 · **Spec:** FR-014, §8.6 · **Fabric hosting:** job (`POST /runs/{id}/report/generate`)

> **Fabric re-test.** Logic unchanged from the first build; status `Needs Fabric re-test`. Run the acceptance criteria below through Fabric: an operation listed as `udf` is called from Rayfin once its screen exists, else from the `nexora_api` test pane (`call(method, path, body=…)`); a `job` operation is started from Rayfin or by reserving it (`POST /runs/{id}/jobs`) and running `nexora_stage_runner` ([CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). Its response is the job result. A file named `runs/<id>/…` is in `Files/runs/<id>/…` of `nexora_control` (`python -m app.cli runs pull <id>` copies it locally). CLI steps run locally against the Fabric stores (`RUN_STORE=fabric_sql`, `ARTIFACT_STORE=onelake`).
> - The PBIR folder is generated in a staging folder and copied to `Files/runs/<id>/report/v<n>/` only after the publish succeeds, as before.

#### Problem statement
The plan must become a real Power BI report bound to the approved semantic model. The generation must be deterministic, so that a report can be repaired one visual at a time later without changing the rest.

#### Functional requirements
1. Generate a PBIR report folder from `ReportPlan.json`, bound by connection to the approved semantic model in the sandbox.
2. Version it per run as `runs/<id>/report/v<n>/`.
3. Publish it to the sandbox by creating the report, or updating it if one with the same name exists.
4. Starting generation moves the run from `MODEL_APPROVED` to `REPORT_GENERATING`.

#### API contracts
- `app/report/pbir_generator.py`: `generate_pbir(plan: ReportPlan, model_item_id: str, out_dir: Path) -> list[Path]`.
- Output layout:
  ```
  runs/<id>/report/v1/
    definition.pbir                     # byConnection → semantic model id
    definition/report.json
    definition/version.json
    definition/pages/pages.json
    definition/pages/<page>/page.json
    definition/pages/<page>/visuals/<visual_id>/visual.json
  ```
- `FabricItemsClient.upsert_report(workspace_id, display_name, pbir_dir) -> {"item_id", "operation"}`, which calls `POST /v1/workspaces/{ws}/reports` with a `PBIR` definition, or `…/reports/{id}/updateDefinition`.
- `POST /runs/{id}/report/generate` returns `200 {"version", "files", "report_item_id", "run": RunDetail}`.

#### Constraints
- Deterministic: the same plan and model ID give byte-identical files. Page and visual folder names come from the plan's IDs.
- Every JSON file declares its PBIR `$schema` and validates against it.
- Sandbox only, through the T-501 target guard.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| A visual type the generator doesn't support | Fails at generation, before publishing, naming the visual. |
| The semantic model ID changed through redeployment | The binding is regenerated from the current model ID. |
| Fabric rejects the definition | `UPSTREAM_ERROR`, with Fabric's error and the request ID. |
| Publish twice | `operation: updated`, and one report in the workspace. |

#### Acceptance criteria
1. **Visible report.** Call `POST /runs/{id}/report/generate`, then open the report in the sandbox workspace. **Expected:** the pages and visuals match the plan, the visuals show data, and the 2024 filter is applied.
2. **Drill works.** On "Net Revenue by Month", drill down from Year to Quarter to Month. **Expected:** drill works, as the plan describes.
3. **Deterministic.** Generate again from the same plan and diff `v1` with `v2`. **Expected:** no difference.
4. **No duplicate.** Publish again. **Expected:** `operation: updated`, and one report.

---

### T-603 — Report validators and Gate G5

**Depends on:** T-602, T-505 · **Spec:** FR-015, FR-016, §12 G5, acceptance scenario "Report binding failure" · **Fabric hosting:** job (`POST /runs/{id}/report/validate`, `POST /runs/{id}/report/repair`)

> **Fabric re-test.** Logic unchanged from the first build; status `Needs Fabric re-test`. Run the acceptance criteria below through Fabric: an operation listed as `udf` is called from Rayfin once its screen exists, else from the `nexora_api` test pane (`call(method, path, body=…)`); a `job` operation is started from Rayfin or by reserving it (`POST /runs/{id}/jobs`) and running `nexora_stage_runner` ([CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). Its response is the job result. A file named `runs/<id>/…` is in `Files/runs/<id>/…` of `nexora_control` (`python -m app.cli runs pull <id>` copies it locally). CLI steps run locally against the Fabric stores (`RUN_STORE=fabric_sql`, `ARTIFACT_STORE=onelake`).
> - Renders are written to `Files/runs/<id>/report/v<n>/renders/`. They are shown by T-604 through the UDF `get_run_file`.

#### Problem statement
A report can publish successfully but still bind to a missing measure or render empty visuals. The spec requires schema, binding and query checks plus rendered output for human review. When a check fails, only the affected visual or page should be repaired, and model defects should be routed back to the model stage.

#### Functional requirements
1. Run these checks:

   | ID | Check |
   |---|---|
   | G5.SCHEMA | Every PBIR file validates against its `$schema` |
   | G5.BIND | Every field and measure in every visual exists in the approved model |
   | G5.QUERY | Each visual's equivalent DAX query runs through `executeQueries`, and returns rows |
   | G5.RENDER | Each page is exported to PNG and saved for review |

2. Classify each failure:
   - **report defect:** the referenced object isn't in the approved ModelSpec, or the PBIR is invalid. Repair the visual or page through T-505.
   - **model defect:** the object is in the ModelSpec, but its query fails. Move the run to `MODEL_GENERATING` with a reason.
3. If any check fails, the run moves to `REPORT_FAILED`. If all pass, it stays in `REPORT_GENERATING`, awaiting review.

#### API contracts
- `POST /runs/{id}/report/validate` returns `200 {"report": ValidationReport run entry, "run": RunDetail}`.
- `POST /runs/{id}/report/repair` returns `200 RepairResult` (from T-505, with `artifact: "Report"`).
- Export: Power BI `POST /v1.0/myorg/groups/{ws}/reports/{id}/ExportTo` with `{"format": "PNG", "powerBIReportConfiguration": {"pages": [{"pageName": "…"}]}}`, then poll and download. Images are saved to `runs/<id>/report/v<n>/renders/<page>.png`.

#### Constraints
- The repair scope is one visual's folder, or one page's folder. The T-505 scope guard applies at that granularity.
- Report repair reuses T-505's `repair` stage graph with `artifact: "Report"`. Routing a model defect to `MODEL_GENERATING` is a run transition, so the validate endpoint makes it after classifying, not a graph node ([CONTRACTS.md#stage-graphs](CONTRACTS.md#stage-graphs)).
- Export needs Fabric or Premium capacity. If it isn't available, `G5.RENDER` is a `warning` with "export unavailable", not a pass.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| A visual query returns 0 rows | `G5.QUERY` warning: "no data", shown in the review. |
| Export times out | `G5.RENDER` warning. The other checks still decide pass or fail. |
| A binding to a measure that was renamed in the model | A report defect. The binding is repaired from the plan. |
| A model measure errors at query time | A model defect. The run is routed to `MODEL_GENERATING`, and the report isn't patched. |

#### Acceptance criteria
1. **Passing report.** Call `POST /runs/{id}/report/validate`. **Expected:** `G5.SCHEMA`, `G5.BIND` and `G5.QUERY` pass, and a PNG for each page is in `renders/`.
2. **Binding failure repaired narrowly.** Edit `visuals/v1/visual.json` to bind `Net Revenue X`, validate (it fails), then call `/report/repair`. **Expected:** only `v1/visual.json` changes between versions, and the validation passes. This is the "Report binding failure" scenario.
3. **Model defect routed back.** Break a measure's DAX in the deployed model and validate the report. **Expected:** classified as a model defect, and the run state is `MODEL_GENERATING` with the reason shown.

---

### T-604 — Report review and approval UI

**Depends on:** T-603 · **Spec:** §12 G5 (human decision), §2.2 (Project reviewer), FR-018 · **Fabric hosting:** ui (Nexora Run → Report tab); review, approvals ACCEPT/REJECT and `get_run_file` are udf; REGENERATE, validate and repair are jobs

> **Fabric rework.** Rewritten for Fabric; status `Not started`. The backend part of this task (services, gates, approvals) is unchanged from the first build and only needs its operations wired through the dispatcher (hosts in [CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). The **UI part is ported** from `rayfin-ui/` (Next.js) to the Rayfin workload `rayfin-workload/` (T-104) as a tab of the **Nexora Run** item editor:
> - The page's display rules move over unchanged (`src/lib/report.ts` and its tests), and the page calls operations through the workload's `src/lib/api.ts` (`call` for `udf`, `runJob` for `job`, with the job progress shown).
> - Every action that is a `job` disables the run's other actions until it finishes, and shows the job's error envelope if it fails.
> - The acceptance criteria are run in Rayfin in the Fabric portal; "Rayfin" below means the workload.
> - `GET /runs/{id}/files/{path}` becomes the UDF operation `get_run_file`: same path rules (no absolute path, `..`, backslash or colon; only files inside the run folder; `404` otherwise), body `{"content_type", "base64", "size"}`. It reads through the artifact store, and files over the UDF response limit recorded in `fabric-hosting.md` are `413` with a link to open the report in Power BI instead. The page shows the image from the base64 body; users never get direct access to `nexora_control`.

#### Problem statement
Technical checks can't judge whether a report is useful. The project reviewer and business user must see the rendered pages and the questions each visual answers. They must be able to regenerate a single visual, and approve the report.

#### Functional requirements
1. The review data covers, for each page: the rendered image, and each visual with its question, binding status and query status.
2. **Regenerate** works on one visual or page, with a guidance comment, through T-505 at that scope.
3. Required approvals are `project_reviewer` and `business_owner`, on the current report hash. When they're complete and G5 passes, the run moves to `REPORT_APPROVED`.
4. A change to the report's hash invalidates the earlier approvals.

#### API contracts
- `GET /runs/{id}/report/review` returns `200 {"report_hash", "pages": [{"name", "image_url", "visuals": [{"id", "title", "questions", "bind_status", "query_status"}]}], "approvals_required", "approvals_done"}`.
- `GET /runs/{id}/files/{path}` serves a file from inside the run folder only, such as render PNGs.
- `POST /runs/{id}/approvals`, with `{"artifact": "Report", "action": "REGENERATE", "target": {"page": "Overview", "visual": "v1"}, "comment": "Use a column chart"}`.
- Workload page: Nexora Run → **Report** (first-build route `/runs/[id]/report`).

#### Constraints
- `GET /runs/{id}/files/{path}` must resolve paths inside `runs/<id>/` only, so traversal is blocked. It's restricted to users who can read the run.
- A regeneration re-runs G5 automatically.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| `path` contains `..`, or an absolute path | `404`. Nothing outside the run folder is served. |
| Render image missing (export unavailable) | A placeholder: "render unavailable — open in Power BI", with a link. |
| A regenerate target that doesn't exist | `404`. |
| The report changes after a partial approval | The approvals are marked invalidated. |

#### Acceptance criteria
1. **Review screen.** Open `/runs/[id]/report`. **Expected:** each page image, with its visuals and the questions they answer.
2. **Single-visual regenerate.** Regenerate `v1` with "use a column chart". **Expected:** only `v1` changes. The image is refreshed, and G5 re-runs.
3. **Traversal blocked.** In the `nexora_api` test pane, call `call("GET", "/runs/<id>/files/..%2F..%2Fconfig%2Froles.yaml")`, then the same with an absolute path and with a backslash. **Expected:** `404` each time, and no file content.
4. **Approve.** Approve as `project_reviewer` and `business_owner`. **Expected:** `REPORT_APPROVED`, with two approval records.
5. **M6 exit.** **Expected:** an approved report artifact exists in the sandbox.
