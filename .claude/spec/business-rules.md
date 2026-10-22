# Business Rules

1. KPI business meaning is approved during requirement analysis, before semantic-model generation.
2. Later semantic-model approval verifies implementation fidelity. It does not redefine the KPI unless the run is returned to requirement analysis.
3. Business users approve definitions and outcomes. Technical validators and engineers verify DAX, TMDL, relationships and bindings.
4. A bridge table may be generated when the mapping keys or approved derivation logic are available. Otherwise, the workflow requests input or creates a data-engineering action.
5. Raw or non-conformed sources require explicit exception approval.
6. An artifact marked **Failed**, **Stale** or **Needs Input** cannot unlock dependent stages.
7. If profile evidence contradicts a proposed relationship, the relationship must be corrected or explicitly overridden with rationale.
8. If data cannot support a KPI, the platform must return **Not Feasible / Data Gap** rather than fabricate a calculation.
