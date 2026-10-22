# nexora_api

The `nexora_api` User Data Functions item: short, deterministic operations
(fabric-hosting.md#operation-hosting-rule). Its real code is `function-app.py`
(T-102), a thin wrapper over `app.fabric.dispatch.dispatch()` — it holds no
business logic. `app.tools.deploy_home` (T-005/T-102) uploads `.platform` and
`function-app.py` as this item's definition; `README.md` itself is excluded.

**Manual step after deploying this item:** add a Manage Connections binding
named `nexora_control_db` (alias) pointing at the `nexora_control_db`
SQLDatabase item, in the Fabric portal. Connections aren't part of the
Git/definition payload, so `deploy_home` can't set this up by itself. A UDF
has no user-delegated Fabric token (confirmed against Microsoft's docs — see
fabric-hosting.md's platform-limits table), so this managed connection is how
the item reaches the control database; `app.auth.get_token` always raises in
this runtime.

**Known gap (see CLAUDE.md "Target hosting" and fabric-hosting.md):** most
stage services still read and write run files (contracts, inventories,
approvals, model/report files, …) through a local path
(`app.fabric.deps.runs_root()`), not through T-101's `ArtifactStore`. That
works inside a job (which has the control Lakehouse mounted or reachable) but
not inside this UDF. Until those modules are retrofitted module by module,
only the operations that touch exclusively the run manifest — `GET /health`,
`POST /runs`, `GET /runs`, `GET /runs/{id}` for a run with no contract or
inventory yet — are expected to work when this item runs for real in Fabric.
