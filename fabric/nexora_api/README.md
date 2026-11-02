# nexora_api

The `nexora_api` User Data Functions item: short, deterministic operations
(fabric-hosting.md#operation-hosting-rule). Its real code is `function_app.py`
(T-102), a thin wrapper over `app.fabric.dispatch.dispatch()` — it holds no
business logic. `app.tools.deploy_home` (T-005/T-102, 2026-11-01 re-check)
uploads `definition.json`, `resources/functions.json`, `function_app.py` and
`.platform` as this item's definition (per Fabric's real definition shape,
confirmed against a live tenant); `README.md` itself is excluded.

**Fixed against a live tenant (2026-11-01):** `function_app.py`'s parameter type hints
were quoted strings (`"fn.FabricSqlConnection"`, `"fn.UserDataFunctionContext"`) --
harmless locally (`fabric.functions` isn't installed outside Fabric, so quoting avoided
an import-time error), but Fabric's own static validator parses annotations literally
and rejects a quoted one, plus every decorator that depends on matching a real-typed
parameter. `query`/`body` also can't carry a Python default value (`= None`); the
`dict | None` union itself also isn't accepted at all (Fabric's validator checks the raw
annotation text against a fixed list of simple types, no `X | Y` syntax); `query`/`body`
are plain `dict` now, with no default. Both are fixed (real, unquoted type references,
no union types, no defaults). `definition.json`'s `libraries.public` also has to declare
`{"name": "fabric-user-data-functions", "type": "PYPI", "version": "1.0"}` -- an empty
`libraries` list let the item create successfully but failed on the next
`updateDefinition` with `FabricUdfLibraryNotFoundInImportList`, so this only surfaced on
a second deploy.

**Known gap, confirmed against a live tenant (2026-11-01):** `resources/functions.json`
is the empty object `{}` -- the richer per-function metadata (HTTP trigger and
`UserDataFunctionContext` bindings, each `fabricFunctionParameters` entry's
`dataType`) made Fabric reject the whole item with `InvalidInput: invalid input
parameter: Type` for a reason this pass couldn't isolate further (the generic
`InvalidInput` response carries no detail). `definition.json` still lists both
real functions by name, so the item itself is fully usable; only the portal's own
test-pane/invocation-code-generation experience loses out until someone works out
the exact accepted `dataType`/binding shape (`function_app.py`'s own decorators
still define the real runtime behavior regardless).

**Manual step after deploying this item:** add a Manage Connections binding
named `nexoracontroldb` (alias -- confirmed against a live tenant, 2026-11-01:
a connection alias may only contain letters and digits, so the originally
documented `nexora_control_db` was rejected; the SQLDatabase item itself keeps
its underscored display name, only the alias changed) pointing at the
`nexora_control_db` SQLDatabase item, in the Fabric portal. Connections aren't
part of the Git/definition payload, so `deploy_home` can't set this up by
itself. A UDF has no user-delegated Fabric token (confirmed against
Microsoft's docs — see fabric-hosting.md's platform-limits table), so this
managed connection is how the item reaches the control database;
`app.auth.get_token` always raises in
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

**Confirmed working end-to-end against a live tenant (2026-11-02):** `me()`,
`GET /health`, `POST /runs`, `GET /runs` and `GET /runs/{id}`, invoked exactly
the way an external application would (Microsoft's "invoke from Python app"
tutorial: a `https://analysis.windows.net/powerbi/api/.default` bearer token
POSTed straight to each function's public URL) — not just "expected to work"
per the known-gap note above, but actually exercised. Getting there needed
four more real fixes beyond the 2026-11-01 pass above:

1. **A UDF has no Environment to attach** (unlike a notebook): `call`/`me`
   failed at invocation with a generic `WorkloadException: NotFound` (no
   detail at all from the external invoke response) until the `nexora` wheel
   was added as this item's own private library instead — `privateLibraries/
   <wheel>.whl` as a definition part, declared in `definition.json`'s
   `libraries.private` (`[{"name": "<wheel>.whl", "type": "WHEEL"}]`), per
   Microsoft's UserDataFunctionDefinition reference. `app.tools.deploy_home`
   now builds the wheel once per run and bundles it into both `nexora_env`
   (for jobs) and this item's definition (for the UDF).
2. **Fabric's UDF Python runtime is 3.11.8.** `pyproject.toml` declared
   `requires-python = ">=3.12"` (matching the local dev venv), so installing
   the wheel failed server-side with `Package 'nexora' requires a different
   Python: 3.11.8 not in '>=3.12'` — visible only in the portal's own
   Develop-mode build log, never in the external invoke response (still just
   a generic `NotFound`). Nothing in the codebase needs 3.12-only syntax, so
   this was relaxed to `>=3.11`.
3. **`app.fabric.deps` (imported unconditionally by `function_app.py`) must
   never import anything that imports `app.llm.client`** — that module
   requires `AZURE_OPENAI_*` at its own import time by design (so a UDF,
   which must never call the model, is unaffected, *as long as it never
   imports it*). `app/discovery/relationship_candidates.py` broke that
   invariant: it imported `normalize` through the legacy
   `app.discovery.requirement_data_matcher`, which pulls in the old prototype
   `app.llm.requirement_analyzer` → `app.llm.client` just for a type hint
   nothing in the real pipeline uses. Fixed to import `normalize` straight
   from `app.discovery.glossary` (what `requirement_data_matcher` itself only
   re-exports). Every other real-pipeline module already deferred its
   `app.llm.structured` import into the function that needs it — this was the
   one place that didn't.
4. **`config/*.yaml` (`roles.yaml`, `glossary.yaml`, …) were never bundled in
   the `nexora` wheel at all** — only files under `app/` were. Every loader
   resolves its file as `PROJECT_ROOT / "config" / "<name>.yaml"`
   (`PROJECT_ROOT` = the installed package's own parent directory), and
   `app/config_files.py`'s Fabric-Files/config/ override only applies in a
   *job*, never in this UDF (which has no token to read OneLake with at all —
   see the module docstring). `config/` is now its own package
   (`config/__init__.py`, `config = ["*.yaml"]` in `pyproject.toml`'s
   `package-data`), so it installs next to `app/` wherever the wheel does,
   giving this UDF a working local fallback the same way local development
   and every offline check already have one.

**Open platform issue, found 2026-11-02, not resolved:** adding a second managed
connection to `call()` (a Lakehouse connection, `nexoracontrol`, to back
`app.store.artifacts.UdfLakehouseArtifactStore` the same way `nexoracontroldb`
backs the SQL-based `RunStore`) never actually took effect, no matter how many
times `deploy_home` redeployed it or the portal republished it. Isolated two
independent ways:
1. A brand-new function (`test_lakehouse`, declared in `definition.json` with
   its own `@udf.function()`) never became callable at all -- querying the live
   item's own function set (`functions present in this function set: call,me`)
   confirmed it, independent of any portal caching.
2. Temporarily swapping `me()`'s own connection and body (an *existing*,
   already-registered function) to use only the Lakehouse connection still ran
   the *original* SQL-based body on external invocation -- and fetching the
   item's stored definition directly through `getDefinition` (bypassing the
   portal entirely) confirmed `function_app.py` itself was still the very
   first version ever deployed, from before any of this session's fixes.

So: `nexora_api`'s wheel (the `app` package content) updates correctly on every
deploy -- that's how the Python-version, import-order and config-bundling fixes
above were all confirmed -- but the item's own definition parts
(`function_app.py`, `definition.json`, `.platform`, `resources/functions.json`)
appear stuck at whatever was first successfully deployed, and neither
`deploy_home`'s `updateDefinition` calls nor the portal's own Publish button
(even from a freshly reopened, non-cached session) change that. The two-connection
code is still committed here, correct and unit-tested
(`app/store/artifacts.py:UdfLakehouseArtifactStore` passes a full fake-based
test), it just isn't live. Until this is understood -- recreating the item from
scratch is the next thing to try, not yet attempted since it would mean
redoing the Manage Connections setup -- a UDF's job-result reads
(`GET /runs/{id}/jobs/{job_id}`), the audit trail and the column-definitions
upload stay on the old gap: no artifact store a UDF can actually use.

Two more things confirmed operationally, not fixable in code:
- **Fabric's dict-typed function parameters reject a JSON `null`.** A
  body-less `call(method, path, query, body: null)` is a `400 InvalidInput`
  naming `parameter_name: "body"`; it has to be `{}`. Fixed in
  `rayfin-workload/src/lib/api.ts`'s `call()`.
- **The `nexoracontroldb` Manage Connections binding needs re-saving in the
  portal after every `deploy_home` run against this item, not just once.**
  Observed directly: the connection resolved (`me()` got past it to a real,
  different error), then the next `updateDefinition` broke it again
  (`InternalError: Unable to load data successfully for fabric item` /
  `Error while getting details for alias 'nexoracontroldb'`), then reopening
  and re-saving the same binding with no other change fixed it again
  immediately. Treat the "add the binding" step in the section above as "add
  or re-confirm the binding" after every redeploy until this is understood
  better.
