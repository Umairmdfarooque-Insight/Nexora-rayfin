# Nexora — Sequential Task Plan

Derived from [`../spec/`](../spec/README.md) and [`../technical-documentation/`](../technical-documentation/README.md). Tasks are ordered so that each one builds only on tasks above it.

> **Redevelopment on Microsoft Fabric (2026-10-21).** The first build of every task ran locally (Next.js UI, FastAPI, `runs/` folder, `.env`). Nexora is now being redeveloped to run entirely on Fabric. The target design is in [fabric-hosting.md](../technical-documentation/fabric-hosting.md), and the operation-to-host map is in [CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations). Each task was edited in place and its status set by what it needs:
> - **`Not started`:** the task was rewritten for Fabric and its code must change. Implement it again with `/implement-task`, review it and run its manual test.
> - **`Needs Fabric re-test`:** the business logic is unchanged and only its entry point moves to a UDF or a job. The task has a **Fabric hosting** line and acceptance criteria reworded for Rayfin on Fabric. `/implement-task` checks the operation is wired through the dispatcher, fixes only what the Fabric notes require, and hands over the acceptance criteria to re-run.
> - **`Done`:** not affected (T-004, T-201).
>
> Old branch names were cleared from the index for reworked tasks; their history is in git and in `../session-context/`. Every task ends with **acceptance criteria** a reviewer can test by hand to confirm it works before the next task starts.

## How to use this plan

- Work top to bottom. Don't start a task until every task in its **Depends on** list has passed its acceptance criteria.
- Shared data shapes, run states, the error envelope, approval records, gate results and validation reports are defined once in [CONTRACTS.md](CONTRACTS.md). Tasks reference that file instead of repeating them.

### Task format

Every task has a header line and six sections:

| Section | Contents |
|---|---|
| **Depends on · Spec · Fabric hosting** | The tasks that must be `Done` first, the spec references (FR/NFR/§), and where the task's operations run in Fabric (`udf`, `job`, `ui` or library). A **Fabric rework** or **Fabric re-test** note under this line wins over older wording in the sections. |
| **Problem statement** | What is wrong or missing today, and why it matters |
| **Functional requirements** | What the task must do, as numbered requirements |
| **API contracts** | Inputs, outputs and data shapes: endpoints, CLI commands, function signatures, file formats and config files |
| **Constraints** | Rules the implementation must follow: spec rules, security, determinism and limits |
| **Edge cases and error handling** | A table of each case and the expected behavior, using the error codes from [CONTRACTS.md](CONTRACTS.md#error-envelope) |
| **Acceptance criteria** | Numbered checks to run by hand. Each has its steps and an **Expected** result. Passing all of them means the task is done. |
- Until the Rayfin workload exists (T-104), acceptance criteria are tested by invoking `nexora_api` from the Fabric portal's function test pane, by starting `nexora_stage_runner` jobs (see [CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)), or with the CLI in local development mode. Swagger and `localhost:8000` are gone from the target design.
- Record progress in the [status tracker](../technical-documentation/roadmap-risks-decisions.md#project-status-tracking) and in the table below. Status values: `Not started` → `In progress` → `Awaiting manual test` → `Done`. A task marked `Needs Fabric re-test` goes straight to `Awaiting manual test` once its wiring is checked, then `Done`.
- To have Claude implement a task, run `/implement-task <milestone file | task ID>` (for example `/implement-task M0-baseline.md` or `/implement-task T-101`). After the acceptance criteria pass, run `/implement-task done T-xxx`.

## Working assumptions for open decisions

The spec leaves these [open](../technical-documentation/roadmap-risks-decisions.md#open-decisions). The plan uses the defaults below so work can start. **Confirm or change them before the task that depends on them.**

| Decision | Default used in this plan | First needed at |
|---|---|---|
| Hosting | **Decided: entirely on Microsoft Fabric.** Rayfin is a Fabric workload (Extensibility Toolkit); short operations are User Data Functions (`nexora_api`); long, model or data operations are notebook jobs (`nexora_stage_runner`); all in a Nexora home workspace per environment. See [fabric-hosting.md](../technical-documentation/fabric-hosting.md). | T-005, T-102, T-104 |
| Backend runtime | **Decided: Python** (the existing `app/` code, packaged as the `nexora` wheel in Fabric Environment `nexora_env`). FastAPI is retired when T-102 is redone. | T-102 |
| Orchestration | **Decided: hybrid.** An explicit state machine with `RunStore` owns run state, approvals and audit. LangGraph stage graphs run the LLM loops inside a stage. See [CONTRACTS.md#stage-graphs](CONTRACTS.md#stage-graphs). | T-101 (state machine), T-202 (first stage graph) |
| Control store | **Decided: Fabric SQL Database for manifests (`RUN_STORE=fabric_sql`, the default in Fabric) plus OneLake Files of the control Lakehouse for every other run file (`ARTIFACT_STORE=onelake`).** The local file store (`RUN_STORE=file`, `ARTIFACT_STORE=local`) stays for development. | T-101 |
| LLM endpoint | **Decided: Azure OpenAI**, as before; the key comes from Key Vault through `notebookutils.credentials.getSecret` in jobs. | T-001, T-705 |
| Telemetry sink | **Default: Eventhouse** (KQL database `nexora_telemetry` in the home workspace), `azuremonitor` optional | T-704 |
| Model authoring route | **Decided: canonical TMDL files generated deterministically (T-502), plus an optional MCP writer behind the same interface.** The MCP server and its tools are not chosen yet, so the writer isn't built. | T-502 |
| Report authoring route | **Decided: deterministic PBIR generator** (confirmed at T-602) | T-602 |
| Sandbox topology | **Decided: one dedicated sandbox workspace per environment** (`SANDBOX_WORKSPACE_ID`) | T-501 |
| Approval roles | **Decided: spec roles in a committed `config/roles.yaml`** (real UPNs in the public repo, one person may hold several roles during development) | T-204 |
| Initial domain | **Decided: Sales** (the existing `saleslt` data) | T-202 |
| Retry limits | **Decided: 3 attempts per repair call** (`max_attempts` in `config/repair_policy.yaml`), plus an earlier stop when an attempt makes no progress | T-505 |

## Task index

| ID | Task | Milestone | Status | Branch |
|---|---|---|---|---|
| T-001 | Dependencies and configuration loading (Variable library, `.env` for dev) | [M0 Baseline](M0-baseline.md) | Awaiting manual test | |
| T-002 | One token provider for Fabric jobs, functions and local dev | M0 | Awaiting manual test | |
| T-003 | Environment smoke check (local and as a Fabric job) | M0 | Awaiting manual test | |
| T-004 | Test fixtures in the sandbox Lakehouse | M0 | Done | task/T-004-test-fixtures-sandbox-lakehouse |
| T-005 | Nexora home workspace and Fabric packaging | M0 | Awaiting manual test | |
| T-101 | Run model, state machine and run store | [M1 Foundation](M1-foundation.md) | Awaiting manual test | |
| T-102 | Fabric orchestration surface (User Data Functions and stage jobs) | M1 | Awaiting manual test | |
| T-103 | Workspace and Lakehouse selection (UI pickers, scope bound by a job) | M1 | Awaiting manual test | |
| T-104 | Rayfin workload shell in Fabric | M1 | Awaiting manual test | |
| T-105 | Fabric identity and attribution | M1 | Awaiting manual test | |
| T-201 | BusinessContract schema | [M2 Requirement control](M2-requirement-control.md) | Done | task/T-201-businesscontract-schema |
| T-202 | Requirement Compiler with ambiguity detection | M2 | Awaiting manual test | |
| T-203 | Clarification loop | M2 | Awaiting manual test | |
| T-204 | Approval service and Gate G1 | M2 | Awaiting manual test | |
| T-205 | Clarification and contract review UI | M2 | Awaiting manual test | |
| T-301 | Metadata Harvester per run | [M3 Data understanding](M3-data-understanding.md) | Awaiting manual test | |
| T-302 | Generic data profiler | M3 | Awaiting manual test | |
| T-303 | Relationship evidence | M3 | Awaiting manual test | |
| T-304 | Gate G2 and inventory UI | M3 | Awaiting manual test | |
| T-401 | Entity-to-source mapping and conformed-table resolver | [M4 Planning](M4-planning.md) | Awaiting manual test | |
| T-402 | KPI feasibility and Data Gap | M4 | Awaiting manual test | |
| T-403 | Bridge-table detection and proposal | M4 | Awaiting manual test | |
| T-404 | ExecutionPlan and ModelSpec generation | M4 | Awaiting manual test | |
| T-405 | Gate G3 and plan review UI | M4 | Awaiting manual test | |
| T-501 | Sandbox deployment client | [M5 Semantic model](M5-semantic-model.md) | Awaiting manual test | |
| T-502 | TMDL generator | M5 | Awaiting manual test | |
| T-503 | DAX measure generation | M5 | Awaiting manual test | |
| T-504 | Model validators and Gate G4 | M5 | Awaiting manual test | |
| T-505 | Validation and Repair service | M5 | Awaiting manual test | |
| T-506 | Bridge-table generation | M5 | Awaiting manual test | |
| T-507 | Model review and approval UI | M5 | Awaiting manual test | |
| T-601 | Report planner | [M6 Report](M6-report.md) | Awaiting manual test | |
| T-602 | PBIR generator and sandbox publish | M6 | Awaiting manual test | |
| T-603 | Report validators and Gate G5 | M6 | Awaiting manual test | |
| T-604 | Report review and approval UI | M6 | Awaiting manual test | |
| T-701 | Impact analysis and stale marking | [M7 Operational controls](M7-operational-controls.md) | Awaiting manual test | |
| T-702 | Promotion deployment, smoke tests and rollback | M7 | Awaiting manual test | |
| T-703 | Audit trail and traceability view | M7 | Awaiting manual test | |
| T-704 | Observability | M7 | Awaiting manual test | |
| T-705 | Security hardening | M7 | Awaiting manual test | |
| T-706 | End-to-end vertical slice acceptance | M7 | Not started | |

## Milestone exit checks

Each milestone ends with the exit deliverable from the [roadmap](../technical-documentation/roadmap-risks-decisions.md#implementation-sequence-and-milestones):

| Milestone | Exit deliverable | Proven by |
|---|---|---|
| M0 | Code runs locally and in Fabric, test data exists, and the home workspace is deployable | T-003, T-004, T-005 |
| M1 | User can initiate and inspect a run from Rayfin in the Fabric portal | T-105 |
| M2 | Approved requirement contract | T-205 |
| M3 | Validated metadata inventory | T-304 |
| M4 | Approved execution and model plan | T-405 |
| M5 | Approved semantic model | T-507 |
| M6 | Approved report artifact | T-604 |
| M7 | Repeatable end-to-end workflow | T-706 |
