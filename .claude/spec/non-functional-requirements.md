# Non-Functional Requirements

| ID | Area | Requirement |
|---|---|---|
| NFR-001 | Security | Use Entra-based authentication (the Fabric sign-in), user-delegated access to Fabric and Power BI, the workspace identity where applicable, least-privilege RBAC and protected secrets (Key Vault). |
| NFR-002 | Isolation | Separate users, workspaces, metadata caches and run artifacts by authorized scope. |
| NFR-003 | Reliability | Use idempotency, bounded retries, checkpoint persistence and recoverable state transitions. |
| NFR-004 | Explainability | Show rationale, evidence and validation details for material recommendations. |
| NFR-005 | Performance | Keep UI interactions responsive, and execute long-running generation through observable, stateful operations: Fabric notebook jobs whose progress the run shows. |
| NFR-006 | Maintainability | Use modular connectors, generators, validators and artifact schemas. |
| NFR-007 | Observability | Capture correlation ID, stage duration, errors, retry count, validation outcome and model-call cost, and keep them in the tenant (a Fabric Eventhouse by default). |
| NFR-008 | Data minimization | Send only required metadata or samples to model endpoints; avoid unnecessary source-data exposure. |
| NFR-009 | Repeatability | The same approved inputs and deterministic configuration should produce structurally consistent outputs. |
| NFR-010 | Extensibility | Support future semantic-model, report and industry-contract modules without redesigning the core workflow. |
