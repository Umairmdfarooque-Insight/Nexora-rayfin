# Acceptance Scenarios

| Scenario | Given | When | Then |
|---|---|---|---|
| Simple Sales KPI | Approved conformed sales tables and clear Net Revenue definition | User submits prompt and approves plan | Validated DAX, semantic model and report draft are produced |
| Ambiguous Revenue | Prompt says Revenue without definition | Requirement analysis detects ambiguity | User selects or defines Revenue before generation proceeds |
| Invalid relationship | Proposed dimension key contains duplicates | Relationship validator executes | Model fails, evidence is shown and impacted model section is regenerated |
| Many-to-many bridge | Product and Promotion require a bridge and mapping logic is available | User approves proposed bridge | Bridge artifact and one-to-many relationships are generated and validated |
| Unsupported bridge | Mapping cannot be derived from metadata or approved rules | Bridge analysis runs | Run returns Needs Input or a data-engineering action; no bridge is guessed |
| Report binding failure | PBIR refers to a missing measure | Report validation runs | Only the affected visual/page is repaired or regenerated |
| Upstream KPI change | Approved Net Revenue formula changes | Impact analysis executes | Model and report artifacts are marked stale and revalidated or regenerated |

## Definition of Done for the Initial Vertical Slice

- [ ] A user submits a Sales or Retail business prompt through Rayfin.
- [ ] A material KPI ambiguity is detected and resolved through clarification.
- [ ] The requirement contract is approved and locked for the run.
- [ ] The selected Lakehouse metadata and relationship evidence are harvested.
- [ ] The model plan is approved, including a fact grain and evidence-backed joins.
- [ ] TMDL and DAX artifacts are generated in a sandbox.
- [ ] At least one intentional model failure demonstrates targeted regeneration and successful revalidation.
- [ ] A PBIR/PBIP report is generated against the approved semantic model.
- [ ] At least one intentional report-binding or visual issue demonstrates repair without rebuilding unaffected artifacts.
- [ ] Business and technical reviewers complete their defined approvals.
- [ ] Approved artifacts are published to the target workspace and smoke tests pass.
- [ ] The run can be traced from final report to prompt, contract, source evidence, model version and validation results.

## Recommended Review Checklist

| Review Area | Questions |
|---|---|
| Requirement | Are KPI meaning, grain, filters, periods, currency and exclusions explicit? |
| Data | Are selected tables approved, fresh enough and supported by profile evidence? |
| Model | Are fact grain, relationships, bridge logic, DAX, RLS and storage mode correct? |
| Report | Does the report answer the approved questions with usable pages and interactions? |
| Validation | Is there evidence for every mandatory check, including reconciliation? |
| Deployment | Is the target authorized and is rollback possible? |
