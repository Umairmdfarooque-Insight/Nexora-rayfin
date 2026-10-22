# Product Definition

## Executive Summary

Rayfin is the user-facing interface for a governed analytics-generation workflow. It is a **Microsoft Fabric workload** that opens inside the Fabric portal, and the whole workflow runs on Fabric. A user enters a business prompt in plain English. Rayfin then coordinates:
- requirement clarification
- Microsoft Fabric Lakehouse metadata analysis
- execution planning
- semantic-model generation
- Power BI report generation
- validation and regeneration
- human approval
- controlled deployment

The main objective is **not** unchecked autonomous generation. It is to reduce repetitive semantic-model and reporting effort while preserving business intent, technical correctness, traceability and human control.

> **Target outcome:** convert an approved business requirement into validated, reviewable and deployable analytics artifacts: a requirement contract, metadata inventory, execution plan, semantic model, report definition and validation evidence.

## Guiding Principles

1. Business prompt first; conversation begins only when clarification is required.
2. No silent assumptions for business-critical definitions such as Revenue, Margin, Retention or Same-Store Sales.
3. The LLM proposes and explains; deterministic services validate, execute and control state transitions.
4. Every major artifact has an explicit validation gate.
5. Failed artifacts are repaired or regenerated and revalidated before progression.
6. Human approval is applied to business meaning, modelling decisions, report usefulness and deployment.
7. Sandbox-first generation and testing before promotion to the target environment.
8. Approved upstream changes mark dependent artifacts as stale and trigger impact-based revalidation or regeneration.

## Target End-to-End Flow

| Stage | Primary Activity | Artifact / Outcome | Control |
|---|---|---|---|
| 1 | Business prompt intake | Business prompt | Input captured in Rayfin |
| 2 | Requirement analysis and clarification | Approved `BusinessContract.yaml` | Business owner approval |
| 3 | Data understanding | `MetadataInventory.json` and profile evidence | Metadata and quality gate |
| 4 | Planning and orchestration | `ExecutionPlan.yaml` and `ModelSpec.yaml` | Model plan approval |
| 5 | Semantic-model generation | TMDL, DAX, relationships, partitions and RLS | Technical validation plus human review |
| 6 | Report generation | `ReportPlan.json` and PBIR/PBIP artifacts | Technical, rendered and business review |
| 7 | Deployment | Published semantic model and report | Final approval and smoke tests |

## Product Statement

Rayfin is a Microsoft Fabric workload (item type **Nexora Run**). Inside the Fabric portal, it provides one guided experience for:
- business-prompt intake
- clarification
- artifact preview
- validation evidence
- human approvals
- workflow status

Rayfin coordinates the underlying AI, deterministic services and Microsoft Fabric / Power BI capabilities. **Rayfin is not itself the LLM or semantic-model engine.**

## Users and Responsibilities

| User Type | Primary Responsibility | Approval Scope |
|---|---|---|
| Business user / KPI owner | Defines business intent and terminology | KPI meaning, filters, grain, exclusions, sample outputs |
| Data / BI engineer | Reviews source use and technical implementation | Table selection, joins, semantic model, DAX and performance |
| Security / platform reviewer | Reviews access and deployment configuration | RLS, workspace target, identities and permissions |
| Project reviewer | Reviews report usefulness and readiness | Pages, visuals, interactions and publication |

## In Scope

- Rayfin screens, as a Fabric workload, for prompts, clarifications, artifact review, validation status and approvals.
- Hosting of the whole workflow on Microsoft Fabric: User Data Functions and notebook jobs for the services, and a Fabric SQL Database and a Lakehouse for run state and artifacts.
- Microsoft Fabric workspace and Lakehouse selection.
- Discovery of schemas, tables, columns and relevant technical metadata.
- Data profiling for keys, nulls, cardinality, freshness, date coverage and relationship evidence.
- Compiling the business requirement into a structured contract.
- Selection of approved conformed tables and creation of a model execution plan.
- Generation or refresh of Power BI semantic-model artifacts using TMDL and DAX.
- Support for Direct Lake and Import design decisions.
- Relationships, hierarchies, formatting, display folders and RLS definitions.
- Bridge-table recommendation and generation when relationship logic is available and approved.
- Power BI report planning and PBIR/PBIP artifact generation.
- Validation, targeted regeneration, human approval, sandbox deployment and promotion.

## Out of Scope for Initial Release

- Unsupervised production mutation without approval.
- Inventing missing business definitions or unsupported KPIs.
- Automatic creation of source data that does not exist, without an approved data-engineering task.
- Guaranteed resolution of every many-to-many scenario without relationship evidence or business rules.
- Replacement of business owners, BI engineers or security reviewers.
- Automatic certification of business results without reconciliation evidence.
