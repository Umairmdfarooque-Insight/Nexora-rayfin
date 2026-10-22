> **Superseded for Fabric.** This evidence was collected on the first, locally hosted build (Next.js, FastAPI, local run folder). It does not count for the Fabric redevelopment; T-706 collects new evidence in `T-706-fabric-evidence.md`.

# T-706 evidence: end-to-end vertical slice acceptance

The offline column was produced by `python -m app.manual_tests.t706_acceptance` (see [MANUAL_TESTING.md](../MANUAL_TESTING.md#t-706--end-to-end-vertical-slice-acceptance)). The live columns are filled in by the people who run the live acceptance in Rayfin against the sandbox and a test "higher" workspace. Never use production.

## Live run

| Item | Value |
|---|---|
| Run ID of the Simple Sales KPI run | |
| Sandbox workspace (ID) | |
| Test "higher" workspace (ID) | |
| Date and who ran it | |
| Screenshots or their paths | |
| Folder of the run (`runs\<run id>`) and files used as evidence | |

## Definition of Done

Tick the live box only when you saw it happen in Rayfin (or the API) on the live run, and write the evidence beside it (a run ID, a file path under `runs\<run id>\`, a screenshot path).

| # | Item | Offline rehearsal | Live | Evidence |
|---|---|---|---|---|
| 1 | A user submits a Sales or Retail business prompt through Rayfin | ✔ (API) | ☐ | |
| 2 | A material KPI ambiguity is detected and resolved through clarification | ✔ | ☐ | |
| 3 | The requirement contract is approved and locked for the run | ✔ | ☐ | |
| 4 | The selected Lakehouse metadata and relationship evidence are harvested | seeded (T-301 to T-304) | ☐ | |
| 5 | The model plan is approved, including a fact grain and evidence-backed joins | seeded (T-404, T-405) | ☐ | |
| 6 | TMDL and DAX artifacts are generated in a sandbox | ✔ | ☐ | |
| 7 | At least one intentional model failure demonstrates targeted regeneration and successful revalidation | ✔ (only `Sales.tmdl` changed) | ☐ | |
| 8 | A PBIR/PBIP report is generated against the approved semantic model | ✔ | ☐ | |
| 9 | At least one intentional report-binding or visual issue demonstrates repair without rebuilding unaffected artifacts | ✔ (only one `visual.json` changed) | ☐ | |
| 10 | Business and technical reviewers complete their defined approvals | ✔ | ☐ | |
| 11 | Approved artifacts are published to the target workspace and smoke tests pass | ✔ | ☐ | |
| 12 | The run can be traced from the final report to the prompt, contract, source evidence, model version and validation results | ✔ | ☐ | |

## Scenarios

| Scenario | First proven in | Offline result | Offline proof | Live result | Run ID |
|---|---|---|---|---|---|
| Simple Sales KPI | T-706 | PASS | `t706_acceptance` part A | | |
| Ambiguous Revenue | T-205 | PASS | `t202`, `t203`, `t204`, part A | | |
| Invalid relationship | T-504 | PASS | `t504`, `t505` | | |
| Many-to-many bridge | T-506 | PASS | `t506` | | |
| Unsupported bridge | T-403 | PASS | `t403` | | |
| Report binding failure | T-603 | PASS | `t603`, part A | | |
| Upstream KPI change | T-701 | PASS | `t701` (a `COMPLETED` run can't be returned, so this is exercised on an approved run) | | |

How to produce each one live is in the T-706 section of [MANUAL_TESTING.md](../MANUAL_TESTING.md).

## Review checklist

The responsible reviewer answers each question for the live run. "Yes" needs a note of what they looked at.

| Review area | Questions | Reviewer | Yes / No | Notes |
|---|---|---|---|---|
| Requirement | Are KPI meaning, grain, filters, periods, currency and exclusions explicit? | Business owner | | |
| Data | Are selected tables approved, fresh enough and supported by profile evidence? | Data engineer | | |
| Model | Are fact grain, relationships, bridge logic, DAX, RLS and storage mode correct? | BI engineer | | |
| Report | Does the report answer the approved questions with usable pages and interactions? | Project reviewer | | |
| Validation | Is there evidence for every mandatory check, including reconciliation? | BI engineer | | |
| Deployment | Is the target authorized and is rollback possible? | Security reviewer / release approver | | |

## Defects found

| # | Scenario | What failed | Commit that fixes it |
|---|---|---|---|
| | | The offline rehearsal found none. Record live defects here, one row each, and fix each in its own commit that names the scenario. After any fix, re-run every scenario. | |

## Status tracker

The [status tracker](../technical-documentation/roadmap-risks-decisions.md#project-status-tracking) lists the evidence for each workstream. Set a workstream to `Done` there only after its live check above passed.
