"""nexora_api: the User Data Functions item for short, deterministic operations
(T-102; fabric-hosting.md#operation-hosting-rule). Handlers hold no business
logic -- they only build this item's run store and artifact store from its own
Manage Connections and call app.fabric.dispatch.dispatch(). The Nexora wheel
(the `app` package) is installed as this item's own private library (T-005's
deploy_home, not an attached Environment -- a UDF has none), confirmed against
a live tenant 2026-11-02.

IMPORTANT (fabric-hosting.md's platform-limits table):
- A UDF has no user-delegated Fabric token at all (confirmed against public
  docs: @udf.generic_connection only mints a token for the item owner's
  identity, and only for the CosmosDb/KeyVault audiences) -- confirmed too
  (2026-11-02): it can't read nexora_settings either (no notebookutils), so
  app.config's Fabric-settings branch never resolves anything inside this
  runtime; only the process environment ever does. Two managed connections
  (Manage Connections, portal-only, not Git/definition-trackable, and
  confirmed 2026-11-02 to need re-saving after every redeploy of this item)
  stand in for both: "nexoracontroldb" (a SQL connection to nexora_control_db)
  backs the RunStore, and "nexoracontrol" (a Lakehouse connection to
  nexora_control -- confirmed 2026-11-02: a connection alias is capped at 20
  characters, so the originally intended "nexoracontrollakehouse" was rejected)
  backs the ArtifactStore -- see app.store.artifacts.UdfLakehouseArtifactStore.
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
  no contract/inventory yet -- plus whatever already goes through the
  ArtifactStore (a job's result, ColumnDefinitions upload, the audit trail)
  are expected to work when this item runs in Fabric. Every other udf:
  operation needs that retrofit first; see CLAUDE.md's "Target hosting"
  section.
"""

import fabric.functions as fn

from app.fabric import deps
from app.fabric.dispatch import dispatch
from app.fabric.identity import principal_from_udf_context
from app.store.artifacts import UdfLakehouseArtifactStore
from app.store.factory import set_artifact_store_override
from app.store.sql_store import SqlRunStore

udf = fn.UserDataFunctions()


def _use_managed_sql_store(sql_connection: fn.FabricSqlConnection) -> None:
    """A SqlRunStore that connects through this item's own Manage Connections
    binding, not through app.config's FABRIC_CONTROL_DB_* + app.auth.get_token
    (a UDF can't get a token itself -- see the module docstring)."""
    deps.set_store_override(SqlRunStore(server="", database="", connect=sql_connection.connect))


def _use_managed_artifact_store(lakehouse_connection: fn.FabricLakehouseClient) -> None:
    """A UdfLakehouseArtifactStore built from this item's own Manage Connections
    binding -- a UDF can't read ARTIFACT_STORE/NEXORA_CONTROL_LAKEHOUSE_ID from
    nexora_settings at all (see the module docstring), so
    app.store.factory.get_artifact_store()'s usual setting-based resolution would
    always fall back to a local store nothing else can see."""
    set_artifact_store_override(UdfLakehouseArtifactStore(lakehouse_connection))


@udf.connection(argName="controlDb", alias="nexoracontroldb")
@udf.connection(argName="controlLakehouse", alias="nexoracontrol")
@udf.context(argName="udfContext")
@udf.function()
def call(
    method: str,
    path: str,
    controlDb: fn.FabricSqlConnection,
    controlLakehouse: fn.FabricLakehouseClient,
    udfContext: fn.UserDataFunctionContext,
    query: dict,
    body: dict,
) -> dict:
    query = query or {}
    body = body if body is not None else None
    principal = principal_from_udf_context(udfContext)
    _use_managed_sql_store(controlDb)
    _use_managed_artifact_store(controlLakehouse)
    result = dispatch(method, path, query, body, principal, as_job=False)
    return {"status": result["status"], "body": result["body"]}


@udf.connection(argName="controlDb", alias="nexoracontroldb")
@udf.context(argName="udfContext")
@udf.function()
def me(controlDb: fn.FabricSqlConnection, udfContext: fn.UserDataFunctionContext) -> dict:
    principal = principal_from_udf_context(udfContext)
    _use_managed_sql_store(controlDb)
    result = dispatch("GET", "/me", {}, None, principal, as_job=False)
    return {"status": result["status"], "body": result["body"]}
