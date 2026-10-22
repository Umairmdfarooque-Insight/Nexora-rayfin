"""nexora_api: the User Data Functions item for short, deterministic operations
(T-102; fabric-hosting.md#operation-hosting-rule). Handlers hold no business
logic -- they only build this item's run store from its own Manage Connections
and call app.fabric.dispatch.dispatch(). The Nexora wheel (the `app` package)
is installed through the Environment nexora_env (T-005), which this item uses.

IMPORTANT, not yet confirmed against a live tenant (fabric-hosting.md's
platform-limits table):
- A UDF has no user-delegated Fabric token at all (confirmed against public
  docs: @udf.generic_connection only mints a token for the item owner's
  identity, and only for the CosmosDb/KeyVault audiences). The managed
  "nexora_control_db" connection below (@udf.connection, Manage Connections)
  is therefore how this item reaches the control SQL database -- NOT
  app.auth.get_token, which always raises in this runtime.
- Every stage service that reads or writes a file under runs/<run_id>/ other
  than RunManifest.json (contracts, inventories, approvals, gates, plans,
  model/report files, audit events) still does so through a local Path
  (app.fabric.deps.runs_root()), not through T-101's ArtifactStore. That is
  fine inside a job (which has a working local/mounted filesystem backed by
  the control Lakehouse) but NOT inside this UDF, which has no such Files/
  mount. Until those modules are retrofitted to go through
  app.store.factory.get_artifact_store() (recommended as part of each
  "Needs Fabric re-test" task's own pass, since each one already touches its
  own stage module), only the operations below that touch exclusively the
  RunStore -- health, POST /runs, GET /runs, and GET /runs/{id} for a run with
  no contract/inventory/data gaps yet -- are expected to work when this item
  actually runs in Fabric. Every other udf: operation needs that retrofit
  first; see CLAUDE.md's "Target hosting" section.
"""

import fabric.functions as fn

from app.fabric import deps
from app.fabric.dispatch import dispatch
from app.fabric.identity import principal_from_udf_context
from app.store.sql_store import SqlRunStore

udf = fn.UserDataFunctions()


def _use_managed_sql_store(sql_connection: "fn.FabricSqlConnection") -> None:
    """A SqlRunStore that connects through this item's own Manage Connections
    binding, not through app.config's FABRIC_CONTROL_DB_* + app.auth.get_token
    (a UDF can't get a token itself -- see the module docstring)."""
    deps.set_store_override(SqlRunStore(server="", database="", connect=sql_connection.connect))


@udf.connection(argName="controlDb", alias="nexora_control_db")
@udf.context(argName="udfContext")
@udf.function()
def call(
    method: str,
    path: str,
    controlDb: "fn.FabricSqlConnection",
    udfContext: "fn.UserDataFunctionContext",
    query: dict | None = None,
    body: dict | None = None,
) -> dict:
    query = query or {}
    body = body if body is not None else None
    principal = principal_from_udf_context(udfContext)
    _use_managed_sql_store(controlDb)
    result = dispatch(method, path, query, body, principal, as_job=False)
    return {"status": result["status"], "body": result["body"]}


@udf.connection(argName="controlDb", alias="nexora_control_db")
@udf.context(argName="udfContext")
@udf.function()
def me(controlDb: "fn.FabricSqlConnection", udfContext: "fn.UserDataFunctionContext") -> dict:
    principal = principal_from_udf_context(udfContext)
    _use_managed_sql_store(controlDb)
    result = dispatch("GET", "/me", {}, None, principal, as_job=False)
    return {"status": result["status"], "body": result["body"]}
