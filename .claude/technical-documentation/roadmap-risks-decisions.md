# Roadmap, Risks and Open Decisions

## Implementation Sequence and Milestones

| Milestone | Workstream | Key Build Items | Exit Deliverable |
|---|---|---|---|
| M1 | Foundation | Rayfin shell, authentication, run store, workspace/Lakehouse selection | User can initiate and inspect a run |
| M2 | Requirement control | Prompt parser, clarification, BusinessContract schema, KPI approval | Approved requirement contract |
| M3 | Data understanding | Metadata adapters, profiling, relationship evidence, inventory UI | Validated metadata inventory |
| M4 | Planning | Entity mapping, table resolver, plan/spec schemas, plan approval | Approved execution and model plan |
| M5 | Semantic model | TMDL/DAX generation, MCP integration, sandbox deployment, model validators | Approved semantic model |
| M6 | Report | Report plan, PBIR/PBIP generation, report validation and rendering | Approved report artifact |
| M7 | Operational controls | Retry, impact analysis, deployment, rollback, telemetry and audit | Repeatable end-to-end workflow |

## Project Status Tracking

Status values: Not started / In progress / Done. The evidence links point to the first, locally hosted build. Every workstream stays `In progress` until the Fabric redevelopment passes T-706.

| Workstream | Status | Evidence / Link | Current Blocker | Next Decision |
|---|---|---|---|---|
| Rayfin UI | In progress | T-104, T-105, T-205, T-304, T-405, T-507, T-604, T-703: `rayfin-ui/` (to be ported to the Fabric workload `rayfin-workload/`), [MANUAL_TESTING.md](../MANUAL_TESTING.md); live acceptance: [T-706-evidence.md](../tasks/T-706-evidence.md) | Fabric redevelopment (see [tasks/README.md](../tasks/README.md)), then live acceptance on Fabric (T-706) | Set to Done when T-706 passes on Fabric |
| Requirement contract | In progress | T-201 to T-205: `app/compiler/`, `app/contracts/business_contract.py`; live acceptance: [T-706-evidence.md](../tasks/T-706-evidence.md) | Fabric redevelopment (see [tasks/README.md](../tasks/README.md)), then live acceptance on Fabric (T-706) | Set to Done when T-706 passes on Fabric |
| Metadata discovery | In progress | T-301, T-303, T-304: `app/discovery/harvester.py`; live acceptance: [T-706-evidence.md](../tasks/T-706-evidence.md) | Fabric redevelopment (see [tasks/README.md](../tasks/README.md)), then live acceptance on Fabric (T-706) | Set to Done when T-706 passes on Fabric |
| Data profiling | In progress | T-302: `app/discovery/data_profiler.py`; live acceptance: [T-706-evidence.md](../tasks/T-706-evidence.md) | Fabric redevelopment (see [tasks/README.md](../tasks/README.md)), then live acceptance on Fabric (T-706) | Set to Done when T-706 passes on Fabric |
| Planning engine | In progress | T-401 to T-405: `app/discovery/model_planner.py`; live acceptance: [T-706-evidence.md](../tasks/T-706-evidence.md) | Fabric redevelopment (see [tasks/README.md](../tasks/README.md)), then live acceptance on Fabric (T-706) | Set to Done when T-706 passes on Fabric |
| Semantic model generation | In progress | T-502, T-503, T-506: `app/model/`; live acceptance: [T-706-evidence.md](../tasks/T-706-evidence.md) | Fabric redevelopment (see [tasks/README.md](../tasks/README.md)), then live acceptance on Fabric (T-706) | Set to Done when T-706 passes on Fabric |
| Semantic model validation | In progress | T-504, T-505: `app/model/validators.py`, `app/repair/`; live acceptance: [T-706-evidence.md](../tasks/T-706-evidence.md) | Fabric redevelopment (see [tasks/README.md](../tasks/README.md)), then live acceptance on Fabric (T-706) | Set to Done when T-706 passes on Fabric |
| Report generation | In progress | T-601, T-602: `app/report/`; live acceptance: [T-706-evidence.md](../tasks/T-706-evidence.md) | Fabric redevelopment (see [tasks/README.md](../tasks/README.md)), then live acceptance on Fabric (T-706) | Set to Done when T-706 passes on Fabric |
| Report validation | In progress | T-603, T-604: `app/report/validators.py`; live acceptance: [T-706-evidence.md](../tasks/T-706-evidence.md) | Fabric redevelopment (see [tasks/README.md](../tasks/README.md)), then live acceptance on Fabric (T-706) | Set to Done when T-706 passes on Fabric |
| Human approvals | In progress | T-204, T-405, T-507, T-604, T-702: `app/workflow/approval.py`; live acceptance: [T-706-evidence.md](../tasks/T-706-evidence.md) | Fabric redevelopment (see [tasks/README.md](../tasks/README.md)), then live acceptance on Fabric (T-706) | Set to Done when T-706 passes on Fabric |
| Sandbox deployment | In progress | T-501, T-702: `app/deploy/`, `app/workflow/promotion.py`; live acceptance: [T-706-evidence.md](../tasks/T-706-evidence.md) | Fabric redevelopment (see [tasks/README.md](../tasks/README.md)), then live acceptance on Fabric (T-706) | Set to Done when T-706 passes on Fabric |
| Fabric hosting | Not started | T-005, T-101, T-102: [fabric-hosting.md](fabric-hosting.md) | — | Set to In progress when T-005 starts |
| Observability and audit | In progress | T-703, T-704, T-705: `app/workflow/audit.py`, `app/observability/`, `app/security/`; live acceptance: [T-706-evidence.md](../tasks/T-706-evidence.md) | Fabric redevelopment (see [tasks/README.md](../tasks/README.md)), then live acceptance on Fabric (T-706) | Set to Done when T-706 passes on Fabric |

## Key Risks and Mitigations

| Risk | Impact | Planned Mitigation |
|---|---|---|
| Incorrect business interpretation | Correct code implements the wrong KPI | Early ambiguity detection, approved contract and sample result checks |
| Poor source metadata | Weak table and relationship recommendations | Profile actual data, show confidence, request input and block low-confidence progression |
| Invalid DAX or TMDL | Model cannot deploy or returns wrong values | Parse, execute, reconcile and use targeted regeneration |
| Many-to-many ambiguity | Incorrect filtering or double counting | Bridge analysis, mapping validation and human model approval |
| LLM variability | Inconsistent proposals | Schema-constrained outputs, deterministic templates, caching and validators |
| Unbounded regeneration | Cost and repeated invalid results | Retry limits, no-progress detection and user escalation |
| Fabric platform limits (UDF timeout and payload size, job start latency, Extensibility Toolkit hosting and tenant registration) | An operation can't run where it was planned, or Rayfin can't be published | The hosting rule in [fabric-hosting.md](fabric-hosting.md); each limit is checked and recorded by the first task that depends on it; long work always runs in jobs |
| User-delegated access only in jobs | UDFs can't check workspace access | Access decisions are made in a job and cached for 5 minutes (T-705) |
| Production impact | Incorrect model/report reaches users | Sandbox-first deployment, approval gates, smoke tests and rollback |
| Scope expansion | Delayed delivery | Milestone exit criteria and one-domain vertical slice first |

## Open Decisions

Hosting and Orchestration are decided. The working assumptions for the others are in [`../tasks/README.md`](../tasks/README.md#working-assumptions-for-open-decisions).

| Decision | Options / Question |
|---|---|
| Hosting | **Decided (2026-10-21): entirely on Microsoft Fabric.** Rayfin is a Fabric workload; User Data Functions, notebook jobs, the control SQL database and the control Lakehouse live in a Nexora home workspace. See [fabric-hosting.md](fabric-hosting.md). |
| Backend runtime | **Decided: Python** (the `nexora` wheel in a Fabric Environment). FastAPI is retired by the Fabric rework (T-102). |
| Orchestration | **Decided: hybrid.** An explicit run state machine with the control store owns run state, approvals and audit. LangGraph stage graphs run the bounded LLM loops inside a stage (compile, DAX generation, repair). See [CONTRACTS.md#stage-graphs](../tasks/CONTRACTS.md#stage-graphs). |
| Control store | **Decided: a Fabric SQL Database for run state, OneLake Lakehouse Files for artifacts.** |
| Model authoring route | Canonical TMDL files, MCP object operations, or combined approach? |
| Report authoring route | PBIR generator, report skill/CLI, or combined approach? |
| Sandbox topology | Dedicated workspace per environment, per team or per run? |
| Approval roles | Who approves KPI, model, security, report and deployment? |
| Initial domain | Sales, Retail or another domain? |
| Retry limits | Maximum attempts by artifact type? |

## Document Approval

The document requires sign-off (Approve / Edit / Reject) from these roles. None have signed yet:
- Business sponsor
- Product / solution owner
- Data engineering reviewer
- Power BI / semantic model reviewer
- Security / platform reviewer
