# Workflow State Model and Validation Gates

## Run States

| State | Meaning |
|---|---|
| `CREATED` | Run created from prompt |
| `NEEDS_CLARIFICATION` | Blocking business question exists |
| `REQUIREMENT_APPROVED` | Business contract approved |
| `DATA_PROFILED` | Metadata and profile evidence available |
| `PLAN_APPROVED` | Execution and model plan approved |
| `MODEL_GENERATING` | Semantic artifacts being created |
| `MODEL_FAILED` | Model validation failed |
| `MODEL_APPROVED` | Model validation and review passed |
| `REPORT_GENERATING` | Report artifact being created |
| `REPORT_FAILED` | Report validation or review failed |
| `REPORT_APPROVED` | Report accepted |
| `DEPLOYING` | Approved artifacts being published |
| `COMPLETED` | Deployment and smoke tests passed |
| `NEEDS_INPUT` | Retry stopped or additional information required |
| `ROLLED_BACK` | Deployment reverted to approved state |

## Validation Gates

| Gate | Automated Checks | Human Decision | Failure Action |
|---|---|---|---|
| G1 Requirement | Schema completeness, unresolved blocking fields, feasibility flags | Approve KPI meaning and boundaries | Clarify, edit or reject |
| G2 Metadata | Source accessibility, metadata completeness, key/profile warnings | Review source suitability when exceptions exist | Reharvest, narrow scope or raise data gap |
| G3 Plan | Grain, approved sources, join evidence, dependency completeness | Approve selected tables, joins, storage and scope | Edit or regenerate plan |
| G4 Semantic Model | TMDL parse, object references, relationship tests, DAX execution, reconciliation, RLS checks | Approve business fidelity and sensitive exposure | Targeted model repair/regeneration |
| G5 Report | PBIR schema, object bindings, query smoke tests, rendered output checks | Approve usability and business questions answered | Regenerate page/visual or return model defect |
| G6 Deployment | Authorized target, approval evidence, idempotency, smoke tests | Final release approval | Retry or rollback |

Approval actions available at checkpoints (FR-018): **Accept, Edit, Reject, Explain, Regenerate**. A retry threshold or no-progress condition moves the run to `NEEDS_INPUT` (FR-017).
