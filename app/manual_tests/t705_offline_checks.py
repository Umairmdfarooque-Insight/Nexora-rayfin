"""T-705 offline checks: secrets from Key Vault, run authorization, the LLM payload guard and the
LangSmith block.

Run: python -m app.manual_tests.t705_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
Key Vault, the model service and Fabric are fakes and the API runs in-process on a temporary folder, so
no sign-in or network is needed. The live steps are in MANUAL_TESTING.md.
"""

import importlib
import io
import json
import logging
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
from fastapi.testclient import TestClient

import app.config as config
from app.api.deps import get_llm, get_runs_root, get_store, get_workspace_lister
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.compiler.critical_terms import ConfigError
from app.compiler.requirement_compiler import build_user_prompt as compile_prompt
from app.contracts import business_contract as bc
from app.discovery import feasibility, source_resolver
from app.llm import client as llm_client
from app.manual_tests import t502_offline_checks as t502
from app.manual_tests import t503_offline_checks as t503
from app.model import dax_generator, recon_sql
from app.observability.logs import CorrelationFilter, JsonFormatter
from app.report import planner
from app.security import llm_guard, run_access, secrets
from app.security.llm_guard import PayloadRejected, check_payload
from app.security.run_access import AccessCache
from app.security.startup import assert_no_external_tracing
from app.store.file_store import FileRunStore
from app.workflow.run import RunScope, new_run

check = t502.check
VAULT = "https://nexora-test-vault.vault.azure.net"
SECRET_VALUE = "s3cret-key-value-123"


class FakeVault:
    def __init__(self, values: dict[str, str], error: Exception | None = None):
        self.values, self.error, self.reads = values, error, []

    def get_secret(self, name):
        self.reads.append(name)
        if self.error:
            raise self.error
        if name not in self.values:
            raise type("ResourceNotFoundError", (Exception,), {})(f"{name} not found")
        return SimpleNamespace(value=self.values[name])


def rows(n=10):
    return [{"ProductID": i, "Name": f"Product {i}", "ListPrice": 10.5 + i} for i in range(n)]


def guard_ok(payload) -> bool:
    try:
        check_payload(payload)
        return True
    except PayloadRejected:
        return False


def main() -> None:
    saved_env = {k: os.environ.get(k) for k in ("KEY_VAULT_URL", "AZURE_OPENAI_API_KEY", "ENTRA_API_CLIENT_SECRET", "LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2")}
    try:
        run_checks()
    finally:
        secrets.reset_for_tests()
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(config)
        app.dependency_overrides.clear()
    print("All checks passed.")


def run_checks() -> None:
    # --- 1. secrets ------------------------------------------------------------------------------------------------
    os.environ.pop("KEY_VAULT_URL", None)
    secrets.reset_for_tests()
    os.environ["AZURE_OPENAI_API_KEY"] = "env-key"
    check("without KEY_VAULT_URL a secret comes from the environment", secrets.load_secret("AZURE_OPENAI_API_KEY", "azure-openai-api-key") == "env-key")
    saved = os.environ.pop("AZURE_OPENAI_API_KEY")
    try:
        secrets.load_secret("AZURE_OPENAI_API_KEY", "azure-openai-api-key")
        raised = None
    except ValueError as exc:
        raised = str(exc)
    check("a missing required secret without a vault is the usual missing-setting error", raised == "Missing required configuration: AZURE_OPENAI_API_KEY. Check your .env file.")
    check("a missing optional secret is None", secrets.load_secret("ENTRA_API_CLIENT_SECRET", "entra-api-client-secret", required=False) is None)

    os.environ["KEY_VAULT_URL"] = VAULT + "/"
    vault = FakeVault({"azure-openai-api-key": SECRET_VALUE, "entra-api-client-secret": "client-secret-value"})
    secrets._client_factory = lambda url: vault
    secrets.reset_for_tests()
    secrets._client_factory = lambda url: vault
    check("with KEY_VAULT_URL the key is read from the vault, not the environment (acceptance 1: the .env value is gone)",
          secrets.load_secret("AZURE_OPENAI_API_KEY", "azure-openai-api-key") == SECRET_VALUE and vault.reads == ["azure-openai-api-key"])
    secrets.load_secret("AZURE_OPENAI_API_KEY", "azure-openai-api-key")
    check("a secret is read once and kept", vault.reads == ["azure-openai-api-key"])
    importlib.reload(config)
    check("app.config takes both secrets from the vault with no AZURE_OPENAI_API_KEY in the environment",
          config.AZURE_OPENAI_API_KEY == SECRET_VALUE and config.ENTRA_API_CLIENT_SECRET == "client-secret-value")
    sparse = FakeVault({"azure-openai-api-key": SECRET_VALUE})
    secrets._client_factory = lambda url: sparse
    secrets._cache.clear()
    check("an optional secret that isn't in the vault is None", secrets.load_secret("ENTRA_API_CLIENT_SECRET", "entra-api-client-secret", required=False) is None)
    try:
        secrets.load_secret("X", "nope", required=True)
        raised = None
    except secrets.SecretsUnavailable as exc:
        raised = str(exc)
    check("a required secret that isn't in the vault names the secret, not a value", raised is not None and "'nope'" in raised and SECRET_VALUE not in raised)

    down = FakeVault({}, error=ConnectionError(f"connection refused while sending {SECRET_VALUE}"))
    secrets._client_factory = lambda url: down
    secrets._cache.clear()
    try:
        importlib.reload(config)
        raised = None
    except ValueError as exc:
        raised = str(exc)
    check("an unreachable vault stops startup with CONFIG_ERROR naming KEY_VAULT_URL and no secret",
          raised is not None and raised.startswith("CONFIG_ERROR") and "KEY_VAULT_URL" in raised and "nexora-test-vault" in raised and SECRET_VALUE not in raised)

    rotating = FakeVault({"azure-openai-api-key": "old-key"})
    secrets._client_factory = lambda url: rotating
    secrets._cache.clear()
    check("a rotated secret is re-read on demand", secrets.load_secret("A", "azure-openai-api-key") == "old-key"
          and (rotating.values.update({"azure-openai-api-key": "new-key"}) or True) and secrets.refresh_secret("azure-openai-api-key") == "new-key"
          and secrets.load_secret("A", "azure-openai-api-key") == "new-key")
    os.environ.pop("KEY_VAULT_URL")
    check("refresh_secret without a vault does nothing", secrets.refresh_secret("azure-openai-api-key") is None)
    check("the service identity is DefaultAzureCredential (the managed identity in Azure, the developer's sign-in locally)",
          type(secrets.service_credential()).__name__ == "DefaultAzureCredential")
    example = Path(".env.example").read_text(encoding="utf-8")
    values = {line.split("=", 1)[0]: line.split("=", 1)[1].strip() for line in example.splitlines() if "=" in line and not line.startswith("#")}
    check(".env.example has KEY_VAULT_URL and no value for any secret", "KEY_VAULT_URL" in values
          and all(not v for k, v in values.items() if any(w in k for w in ("KEY", "SECRET", "PASSWORD", "CONNECTION_STRING"))))

    # --- 2. the payload guard ------------------------------------------------------------------------------------------------
    check("acceptance 3: a payload with a list of 10 row dictionaries is rejected", not guard_ok({"instructions": "x", "input": json.dumps({"sample": rows(10)})}))
    try:
        check_payload({"input": json.dumps({"data": rows(10)})})
    except PayloadRejected as exc:
        reason = exc.reason
    check("the reason names where the rows are and says what they look like, without a value", "payload.input.data" in reason and "rows" in reason and "Product 3" not in reason)
    check("the same rows as a direct dict, after other text in the input, or as a matrix are rejected too",
          not guard_ok({"data": rows(6)}) and not guard_ok({"input": "Context.\n\n" + json.dumps(rows(7))})
          and not guard_ok({"input": json.dumps({"values": [[i, "a", 1.5] for i in range(8)]})}))
    check("a field called rows or samples with content is rejected, even with a single row", not guard_ok({"rows": [{"a": 1}]}) and not guard_ok({"x": {"sample_rows": [1]}}))
    check("a pasted table in free text is rejected", not guard_ok({"input": "Here is data:\n" + "\n".join(f"{i},Name {i},{i * 2.5},EU" for i in range(14))}))
    check("a payload over the size limit is rejected", not guard_ok({"input": "x" * (llm_guard.MAX_CHARS + 1)}))
    check("a few records (under 5) and an empty rows field are allowed", guard_ok({"x": rows(4)}) and guard_ok({"rows": []}))
    columns = [{"name": f"Col{i}", "data_type": "string"} for i in range(60)]
    check("metadata lists of any length pass: table and column names and types, KPIs, measures, questions",
          guard_ok({"tables": [{"table": "s.t", "columns": [{"name": c["name"], "type": "int"} for c in columns]}] * 8, "kpis": [{"name": f"K{i}", "formula_intent": "x"} for i in range(9)]}))
    check("profile summaries (counts, min and max) pass", guard_ok({"profile": {"row_count": 1200, "columns": {"A": {"null_count": 1, "distinct_count": 5, "min": 0.5, "max": 9}}}}))

    spec = t503.spec_with_measures()
    inventory = t502.build_inventory(spec)
    contract = bc.BusinessContract(run_id="r", business_objective="Revenue", entities=["Sales"], dimensions=["Product", "Month", "Region", "Channel", "Year"],
                                   kpis=[t503.KPI_NR, t503.KPI_MON, bc.Kpi(name="A", formula_intent="a", grain="g", currency="USD"), bc.Kpi(name="B", formula_intent="b", grain="g", currency="USD"),
                                         bc.Kpi(name="C", formula_intent="c", grain="g", currency="USD"), bc.Kpi(name="D", formula_intent="d", grain="g", currency="USD")])
    real = {
        "source resolution": json.dumps(source_resolver.build_payload(contract, inventory, {"Sales": ["orders", "revenue"]})),
        "feasibility": json.dumps(feasibility.build_payload(contract, {f"s.t.C{i}": "int" for i in range(12)})),
        "dax": dax_generator.build_user_prompt(t503.KPI_NR, spec, t503.NR),
        "dax retry": dax_generator.build_user_prompt(t503.KPI_NR, spec, t503.NR, ["unknown column 'X'"]),
        "reconciliation": recon_sql.build_user_prompt(t503.KPI_NR, spec),
        "report plan": planner.build_user_prompt(planner.build_payload(contract, spec, [], ["Sales leadership"], ["report"])),
        "compile (with a previous contract)": compile_prompt(new_run("Show revenue by month"), contract, []),
    }
    for name, text in real.items():
        check(f"the real {name} payload passes the guard", guard_ok({"instructions": "You are Nexora.", "input": text}))
    check("every system prompt of the pipeline passes the guard", all(
        guard_ok({"instructions": s, "input": "u"}) for s in (source_resolver.SYSTEM_PROMPT, feasibility.SYSTEM_PROMPT, dax_generator.SYSTEM_PROMPT, recon_sql.SYSTEM_PROMPT, planner.SYSTEM_PROMPT)))

    # --- 3. the shared client -------------------------------------------------------------------------------------------------
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(CorrelationFilter())
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("app")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    sent: list[dict] = []
    state = {"keys": []}

    def make_raw():
        key = llm_client._state["key"]
        state["keys"].append(key)

        class Responses:
            def create(self, **kwargs):
                sent.append(kwargs)
                if key == "old-key":
                    request = httpx.Request("POST", "https://x/openai/v1/responses")
                    raise openai.AuthenticationError("bad key", response=httpx.Response(401, request=request), body=None)
                return SimpleNamespace(output_text="ok")

        return SimpleNamespace(responses=Responses())

    original_raw = llm_client._raw
    llm_client._raw = lambda: llm_client._state.setdefault("raw", None) or make_raw()
    llm_client._state["key"] = "good-key"
    try:
        llm_client.client.responses.create(model="m", instructions="system", input=json.dumps({"tables": [{"table": "a", "columns": [{"name": "x", "type": "int"}]}]}))
        check("an allowed payload is sent", len(sent) == 1)
        sent.clear()
        try:
            llm_client.client.responses.create(model="m", instructions="system", input=json.dumps({"customers": rows(10)}))
            blocked = False
        except PayloadRejected:
            blocked = True
        check("a payload with rows is rejected by the shared client and no request is sent", blocked and sent == [])
        lines = [json.loads(x) for x in stream.getvalue().splitlines() if x.startswith("{")]
        ok_line = next(x for x in lines if x["msg"] == "model payload")
        bad_line = next(x for x in lines if x["msg"] == "model payload rejected")
        check("the log has the size and shape of an allowed payload, and the reason for a rejected one, and no content",
              ok_line["payload_chars"] > 0 and ok_line["payload_shape"]["input"].startswith("str[") and "reason" in bad_line and "customers" in bad_line["reason"]
              and "Product 3" not in stream.getvalue())

        # a 401 re-reads the key from Key Vault and retries once
        os.environ["KEY_VAULT_URL"] = VAULT
        rotated = FakeVault({"azure-openai-api-key": "new-key"})
        secrets._client_factory = lambda url: rotated
        secrets._cache.clear()
        llm_client._state["key"], sent[:] = "old-key", []
        result = llm_client.client.responses.create(model="m", instructions="s", input="hi")
        check("a 401 re-reads the key from Key Vault and retries once, with the new key", result.output_text == "ok" and len(sent) == 2
              and rotated.reads == ["azure-openai-api-key"] and llm_client._state["key"] == "new-key")
        os.environ.pop("KEY_VAULT_URL")
        llm_client._state["key"], sent[:] = "old-key", []
        try:
            llm_client.client.responses.create(model="m", instructions="s", input="hi")
            failed = False
        except openai.AuthenticationError:
            failed = True
        check("a 401 with no vault is not retried", failed and len(sent) == 1)
    finally:
        llm_client._raw = original_raw
        logger.removeHandler(handler)

    # --- 4. run authorization -------------------------------------------------------------------------------------------------------
    who = {"oid": "oid-a", "upn": "a@contoso.com"}
    accessible: list[list[dict]] = [[]]
    lister_calls = {"n": 0}

    def lister():
        lister_calls["n"] += 1
        if accessible[0] is None:
            raise RuntimeError("Fabric is down")
        return accessible[0]

    clock = {"t": 0.0}
    run_access.cache = AccessCache(clock=lambda: clock["t"])
    with tempfile.TemporaryDirectory() as tmp:
        runs_root = Path(tmp) / "runs"
        store = FileRunStore(runs_root)
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        app.dependency_overrides[get_workspace_lister] = lambda: lister
        app.dependency_overrides[authenticate] = lambda: AuthContext(principal=Principal(oid=who["oid"], upn=who["upn"], name="N"), token="x")
        client = TestClient(app, raise_server_exceptions=False)

        def envelope(r, status, code):
            return r.status_code == status and r.json()["error"]["code"] == code

        r = client.post("/runs", json={"prompt": "Show revenue by month"})
        rid = r.json()["run_id"]
        check("the creator can read and act on their run", client.get(f"/runs/{rid}").status_code == 200 and lister_calls["n"] == 0)
        who.update(oid="oid-b", upn="b@contoso.com")
        r = client.get(f"/runs/{rid}")
        check("acceptance 2: another user with no access to the run's workspace gets 403 FORBIDDEN", envelope(r, 403, "FORBIDDEN"))
        check("they can't act on it either (compile, approvals, files, impact, audit)", all(
            code == 403 for code in (client.post(f"/runs/{rid}/compile").status_code, client.get(f"/runs/{rid}/audit").status_code,
                                     client.get(f"/runs/{rid}/impact").status_code, client.get(f"/runs/{rid}/files/x").status_code,
                                     client.post(f"/runs/{rid}/approvals", json={"artifact": "BusinessContract", "action": "ACCEPT", "expected_hash": "x"}).status_code)))
        check("an unknown run is still 404", envelope(client.get("/runs/00000000-0000-4000-8000-000000000000"), 404, "RUN_NOT_FOUND"))
        check("the run list of the other user doesn't show it", client.get("/runs").json()["items"] == [] and client.get("/runs").json()["total"] == 0)

        run = store.get(rid)
        run.scope = RunScope(workspace_id="WS-1", lakehouse_id="lh", lakehouse_name="L")
        store.save(run)
        check("a scoped run is still refused while the user can't see its workspace", envelope(client.get(f"/runs/{rid}"), 403, "FORBIDDEN"))
        accessible[0] = [{"id": "ws-1"}]
        clock["t"] = 400
        check("a user who can access the run's workspace may read it (ids compared ignoring case)", client.get(f"/runs/{rid}").status_code == 200 and rid in [x["run_id"] for x in client.get("/runs").json()["items"]])
        calls = lister_calls["n"]
        accessible[0] = []
        clock["t"] = 400 + 299
        check("the answer is cached for 5 minutes: removed from the workspace, still in", client.get(f"/runs/{rid}").status_code == 200 and lister_calls["n"] == calls)
        clock["t"] = 400 + 301
        check("after 5 minutes the check runs again and the user has lost the run", envelope(client.get(f"/runs/{rid}"), 403, "FORBIDDEN") and lister_calls["n"] == calls + 1)
        clock["t"] = 1100
        accessible[0] = None
        check("a Fabric that can't be read is a refusal, not a pass", envelope(client.get(f"/runs/{rid}"), 403, "FORBIDDEN"))
        accessible[0] = [{"id": "WS-1"}]
        check("and a failure isn't cached: the next call checks again", client.get(f"/runs/{rid}").status_code == 200)

        run_access.cache.clear()
        before = len(run_access.cache._items)
        run_access.ENTRA_TENANT_ID = "tenant-1"
        client.get(f"/runs/{rid}")
        run_access.ENTRA_TENANT_ID = "tenant-2"
        client.get(f"/runs/{rid}")
        run_access.ENTRA_TENANT_ID = None
        keys = list(run_access.cache._items)
        check("the cache is scoped by tenant, workspace and user", before == 0 and len(keys) == 2 and {k[0] for k in keys} == {"tenant-1", "tenant-2"}
              and all(k[1] == "ws-1" and k[2] == "oid-b" for k in keys))

        no_scope = store.create("Mine").run_id
        run = store.get(no_scope)
        run.created_by = {"oid": "oid-a", "upn": "a@contoso.com", "name": "A"}
        store.save(run)
        check("an owned run with no scope is the owner's alone", envelope(client.get(f"/runs/{no_scope}"), 403, "FORBIDDEN"))
        unowned = store.create("From the CLI").run_id
        check("a run made through the CLI has no owner and stays open to signed-in users", client.get(f"/runs/{unowned}").status_code == 200)
        who.update(oid="oid-a", upn="A@Contoso.com")
        check("the creator is recognized by oid or by upn, ignoring case", client.get(f"/runs/{no_scope}").status_code == 200)

        # --- 5. a rejected payload fails the stage with 422 ---------------------------------------------------------------------
        def rows_llm(system, user, schema):
            check_payload({"instructions": system, "input": json.dumps({"rows": rows(10)})})
            return "{}"

        app.dependency_overrides[get_llm] = lambda: rows_llm
        fresh = client.post("/runs", json={"prompt": "Show revenue by month"}).json()["run_id"]
        r = client.post(f"/runs/{fresh}/compile")
        check("a payload rejected by the guard fails the stage with 422 PAYLOAD_REJECTED and the run is unchanged",
              envelope(r, 422, "PAYLOAD_REJECTED") and "reason" in r.json()["error"]["details"] and store.get(fresh).state.value == "CREATED")

    # --- 6. LangSmith ---------------------------------------------------------------------------------------------------------------
    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"):
        os.environ[name] = "true"
        try:
            assert_no_external_tracing()
            raised = None
        except ConfigError as exc:
            raised = str(exc)
        check(f"acceptance 5: {name}=true is CONFIG_ERROR naming the setting", raised is not None and raised.startswith("CONFIG_ERROR") and name in raised)
        try:
            with TestClient(app):
                started = True
        except ConfigError:
            started = False
        check(f"the API doesn't start with {name}=true", not started)
        try:
            importlib.reload(llm_client)
            loaded = True
        except ConfigError:
            loaded = False
        check(f"the shared client doesn't load with {name}=true", not loaded)
        os.environ[name] = "false"
        assert_no_external_tracing()
        os.environ.pop(name)
    importlib.reload(llm_client)
    with TestClient(app) as started_client:
        check("with tracing off the API starts", started_client.get("/health").status_code == 200)

    # --- 7. the SQL-backed access cache and default_cache() selection (T-705's Fabric rework) -------------------
    import sqlite3

    from app.security.run_access import SqlAccessCache, can_access, cached_decision, default_cache

    class FakeAccessConnection:
        def __init__(self, db):
            self.db = db

        def cursor(self):
            return FakeAccessCursor(self.db)

        def commit(self):
            self.db.commit()

        def rollback(self):
            self.db.rollback()

        def close(self):
            pass

    class FakeAccessCursor:
        def __init__(self, db):
            self.db = db
            self.cur = None
            self.rowcount = 0

        def execute(self, sql, *params):
            if "OBJECT_ID" in sql:
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS nexora_run_access (tenant_id TEXT, workspace_id TEXT, "
                    "user_oid TEXT, allowed INTEGER, checked_at REAL, "
                    "PRIMARY KEY (tenant_id, workspace_id, user_oid))"
                )
                return
            self.cur = self.db.execute(sql.replace("dbo.", ""), params)
            self.rowcount = self.cur.rowcount

        def fetchone(self):
            return self.cur.fetchone()

    access_db = sqlite3.connect(":memory:")
    access_clock = {"t": 0.0}
    sql_cache = SqlAccessCache("server", "db", clock=lambda: access_clock["t"], connect=lambda: FakeAccessConnection(access_db))
    key = ("tenant-x", "ws-x", "oid-x")
    check("SqlAccessCache: a miss is None", sql_cache.get(key) is None)
    sql_cache.set(key, True)
    check("SqlAccessCache: a fresh row reads back true", sql_cache.get(key) is True)
    sql_cache.set(key, False)
    check("SqlAccessCache: set overwrites the same key (upsert, not a duplicate row)", sql_cache.get(key) is False)
    access_clock["t"] = run_access.TTL_SECONDS + 1
    check("SqlAccessCache: a row past the ttl is a miss again", sql_cache.get(key) is None)
    access_clock["t"] = 0.0
    sql_cache.set(key, True)
    sql_cache.clear()
    check("SqlAccessCache: clear empties the table", sql_cache.get(key) is None)

    os.environ.pop("RUN_STORE", None)
    run_access._sql_cache = None
    check("default_cache(): RUN_STORE unset (the default, file) returns the in-memory cache", default_cache() is run_access.cache)
    os.environ["RUN_STORE"] = "fabric_sql"
    os.environ["FABRIC_CONTROL_DB_SERVER"] = "server.database.fabric.microsoft.com"
    os.environ["FABRIC_CONTROL_DB_NAME"] = "nexora_control_db"
    got = default_cache()
    check("default_cache(): RUN_STORE=fabric_sql returns a SqlAccessCache, built once", isinstance(got, SqlAccessCache) and default_cache() is got)
    os.environ.pop("RUN_STORE", None)
    os.environ.pop("FABRIC_CONTROL_DB_SERVER", None)
    os.environ.pop("FABRIC_CONTROL_DB_NAME", None)
    run_access._sql_cache = None

    run_access.cache = AccessCache()
    scoped_run = SimpleNamespace(created_by={"oid": "owner-oid", "upn": "owner@contoso.com"}, scope=RunScope(workspace_id="WS-9", lakehouse_id="lh", lakehouse_name="L"))
    check("cached_decision(): no fresh row yet is None, never calling Fabric", cached_decision(scoped_run, "other-oid") is None)
    can_access(scoped_run, "other-oid", "other@contoso.com", lambda: [{"id": "ws-9"}])
    check("cached_decision(): reads back what can_access (a job) just wrote", cached_decision(scoped_run, "other-oid") is True)
    unscoped_run = SimpleNamespace(created_by={"oid": "owner-oid", "upn": "owner@contoso.com"}, scope=None)
    check("cached_decision(): an unscoped run is always None (nothing to cache)", cached_decision(unscoped_run, "other-oid") is None)

    # --- 8. the job:check_access dispatcher operation (T-705) ------------------------------------------------------
    from app.fabric import deps as fabric_deps
    from app.fabric.dispatch import dispatch as fabric_dispatch

    with tempfile.TemporaryDirectory() as tmp2:
        runs_root2 = Path(tmp2) / "runs"
        store2 = FileRunStore(runs_root2)
        run2 = store2.create("Show revenue by month")
        run2.created_by = {"oid": "owner-oid-2", "upn": "owner2@contoso.com", "name": "Owner"}
        run2.scope = RunScope(workspace_id="WS-42", lakehouse_id="lh", lakehouse_name="L")
        store2.save(run2)
        other2 = {"oid": "other-oid-2", "upn": "other2@contoso.com", "name": "Other"}

        fabric_deps._default_store.cache_clear()
        fabric_deps.set_store_override(store2)
        original_lister = fabric_deps.workspace_lister
        fabric_deps.workspace_lister = lambda: (lambda: [{"id": "ws-42"}])
        run_access.cache = AccessCache()
        try:
            r = fabric_dispatch("GET", f"/runs/{run2.run_id}", {}, None, other2, as_job=False)
            check("a non-owner UDF call with no cached decision yet: 403 ACCESS_CHECK_REQUIRED", envelope_dict(r, 403, "ACCESS_CHECK_REQUIRED"))
            r = fabric_dispatch("POST", f"/runs/{run2.run_id}/check_access", {}, {}, other2, as_job=True)
            check("job:check_access: allowed, and the decision is written", r["status"] == 200 and r["body"]["allowed"] is True and "checked_at" in r["body"])
            r = fabric_dispatch("GET", f"/runs/{run2.run_id}", {}, None, other2, as_job=False)
            check("after check_access, the same UDF call now succeeds from the cached decision alone", r["status"] == 200)

            fabric_deps.workspace_lister = lambda: (lambda: [])
            run_access.cache = AccessCache()
            r = fabric_dispatch("POST", f"/runs/{run2.run_id}/check_access", {}, {}, other2, as_job=True)
            check("job:check_access: not allowed is recorded too (not just a refusal to cache)", r["status"] == 200 and r["body"]["allowed"] is False)
            r = fabric_dispatch("GET", f"/runs/{run2.run_id}", {}, None, other2, as_job=False)
            check("a fresh 'not allowed' row is 403 FORBIDDEN straight away, no further job needed", envelope_dict(r, 403, "FORBIDDEN"))
        finally:
            fabric_deps.workspace_lister = original_lister
            fabric_deps.set_store_override(None)
            fabric_deps._default_store.cache_clear()


def envelope_dict(r: dict, status: int, code: str) -> bool:
    return r["status"] == status and r["body"]["error"]["code"] == code


if __name__ == "__main__":
    main()
