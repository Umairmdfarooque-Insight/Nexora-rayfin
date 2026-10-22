# Artifact Contracts

| Artifact | Minimum Contents | Producer | Primary Validator |
|---|---|---|---|
| `BusinessContract.yaml` | Domain, grain, KPIs, dimensions, filters, currency, exclusions, security, open questions | Requirement Compiler | JSON/YAML schema plus approval completeness rules |
| `MetadataInventory.json` | Sources, tables, columns, datatypes, profiles, quality and relationship evidence | Metadata Harvester | Source reconciliation and profile checks |
| `ExecutionPlan.yaml` | Selected sources, dependencies, stages, gates, targets and approvals | Planning Engine | Policy and feasibility rules |
| `ModelSpec.yaml` | Tables, roles, partitions, relationships, measures, formatting, RLS | Planning / Semantic Model Builder | Spec schema and reference checks |
| TMDL project | Semantic-model definition | Semantic Model Builder | TMDL parser, object validator, DAX tests |
| `ReportPlan.json` | Pages, questions, users, visuals, filters, interactions | Report Planner | Plan schema and semantic object references |
| PBIR/PBIP | Report definition and bindings | Report Builder | PBIR schema, binding, query and rendered review |
| `ValidationReport.json` | Checks, evidence, severity, errors, remediation and approval state | Validation Service | Completeness and status rules |
| `RunManifest.json` | Run ID, versions, hashes, stage state, deployment target and outcome | Orchestrator | State-transition and audit rules |
