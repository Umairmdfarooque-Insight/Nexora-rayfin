# Functional Requirements

| ID | Requirement | Description | Acceptance Summary |
|---|---|---|---|
| FR-001 | Business prompt intake | Rayfin shall capture a plain-English business requirement, requested subject area, desired outputs and optional constraints. | A submitted prompt creates a uniquely identified run. |
| FR-002 | Clarification control | The system shall identify material ambiguities and ask targeted questions before generation. | A run cannot progress while a blocking ambiguity remains open. |
| FR-003 | KPI definition approval | The system shall capture KPI formula intent, grain, filters, time logic, exclusions, currency and expected examples. | The business owner can accept, edit or reject the KPI definition. |
| FR-004 | Workspace and Lakehouse selection | Rayfin shall allow selection of the permitted Fabric workspace and Lakehouse. | Only resources available to the authenticated identity are shown. |
| FR-005 | Metadata discovery | The system shall collect schemas, tables, columns, datatypes, descriptions and relevant source metadata. | A metadata inventory is produced for the selected scope. |
| FR-006 | Data profiling | The system shall calculate or retrieve evidence for row count, nulls, distinct values, key uniqueness, freshness and date coverage. | Profile results include status, evidence and warnings. |
| FR-007 | Relationship analysis | The system shall evaluate key compatibility, value overlap, cardinality and orphan rates. | Each proposed relationship includes evidence and confidence. |
| FR-008 | Bridge-table handling | The system shall detect potential many-to-many patterns and propose a bridge design. | The bridge is generated only after approval and only when mapping logic is available. |
| FR-009 | Conformed-table resolution | The system shall prefer approved conformed tables and explain selected and rejected sources. | The execution plan records table-selection rationale. |
| FR-010 | Semantic-model planning | The system shall define fact grain, dimensions, relationships, storage mode, measures, hierarchies and RLS requirements. | The model plan is reviewable before generation. |
| FR-011 | TMDL and DAX generation | The system shall generate the initial semantic-model definition and measures from the approved contract and metadata. | Generated artifacts are syntactically structured and versioned per run. |
| FR-012 | Semantic-model validation | The system shall validate object references, relationships, cardinality, DAX execution, RLS definitions and reconciliation tests. | A failed model cannot progress to report generation. |
| FR-013 | Report planning | The system shall define business questions, intended users, pages, visuals, filters, drill paths and interaction intent. | A report plan is produced from the approved semantic model. |
| FR-014 | PBIR/PBIP generation | The system shall generate report artifacts bound only to approved semantic objects. | Every bound field and measure exists in the approved model. |
| FR-015 | Report validation | The system shall run schema, binding and query checks and support rendered-page review. | Technical and human review statuses are recorded. |
| FR-016 | Regeneration loop | The system shall feed structured errors and approved constraints into targeted repair or regeneration. | Only failed or impacted artifacts are regenerated where possible. |
| FR-017 | Bounded retry and escalation | The system shall detect repeated failure or no progress and escalate to a user. | A retry threshold or no-progress condition moves the run to Needs Input. |
| FR-018 | Human approvals | The system shall support Accept, Edit, Reject, Explain and Regenerate actions at defined checkpoints. | Every approval is attributable and time-stamped. |
| FR-019 | Sandbox-first deployment | The system shall create and test artifacts in a controlled non-production target before promotion. | Production deployment requires passed gates and explicit approval. |
| FR-020 | Impact analysis | The system shall mark dependent artifacts stale when an approved upstream artifact changes. | Affected artifacts are listed and revalidated or regenerated. |
| FR-021 | Run visibility | Rayfin shall display run stage, artifact status, validation result, open questions and next action. | A user can identify current state and blocker from one screen. |
| FR-022 | Audit trail | The system shall retain prompts, approved definitions, artifact versions, validation results and deployment outcomes. | The final report can be traced to its requirement and source evidence. |
