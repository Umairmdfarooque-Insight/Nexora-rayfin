# LLM, MCP and Deterministic Responsibility Split

| Capability | LLM Role | MCP / Tool Role | Deterministic Role |
|---|---|---|---|
| KPI logic | Interpret approved definition and propose DAX | Create/inspect measure objects where configured | Parse, execute, reconcile and enforce tests |
| TMDL model | Propose structure and descriptions | Create/update semantic-model objects | Serialize, parse, validate references and control deployment |
| Relationships | Propose candidates and explain rationale | Create relationship objects | Test uniqueness, cardinality, value overlap and orphan rate |
| Bridge tables | Propose design from evidence and approved rules | Create model objects; invoke approved data-engineering operation when defined | Validate mapping uniqueness, duplicates and relationship behavior |
| Report | Propose pages, visuals and narrative | Author PBIR/PBIP objects | Validate schema, bindings and queries; render for review |
| Regeneration | Interpret errors and propose corrected section | Apply targeted object changes | Enforce allowed scope, retry limits and pass/fail status |

> **Design decision:** the LLM may create candidate DAX/TMDL content or invoke MCP tools, but it does not independently decide what is accepted or deployed. Deterministic validators and workflow state govern progression.
