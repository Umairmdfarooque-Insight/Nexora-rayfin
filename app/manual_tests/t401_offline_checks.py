"""T-401 offline checks: the glossary, entity-to-source scoring, the conformed-table policy and
the POST /runs/{id}/plan/sources endpoint.

Run: python -m app.manual_tests.t401_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
The Lakehouse, SQL endpoint and model are fakes, and roles, conformed sources and the glossary
are temporary files (extending the fixtures of t302 and t303), so no sign-in or network is
needed. The live steps are in MANUAL_TESTING.md.
"""

import json
import tempfile
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import (
    get_connector_factory,
    get_runs_root,
    get_source_llm,
    get_sql_connector_factory,
    get_store,
)
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.compiler.critical_terms import ConfigError
from app.contracts import business_contract as bc
from app.contracts.metadata_inventory import InventoryColumn, InventoryScope, InventoryTable, MetadataInventory
from app.discovery import glossary as glossary_module
from app.discovery.glossary import Glossary, load_glossary
from app.discovery.requirement_data_matcher import match_requirements_to_data
from app.discovery.source_resolver import Suggestions, resolve
from app.llm.requirement_analyzer import RequirementSpec
from app.manual_tests import t302_offline_checks as base
from app.manual_tests import t303_offline_checks as t303  # extends the t302 fixtures
from app.store.file_store import FileRunStore
from app.workflow import conformed as conformed_module
from app.workflow import roles as roles_module
from app.workflow.run import RunScope, SqlEndpoint
from app.workflow.states import RunState

check = base.check
col = base.col
WS, LH, RANDOM = base.WS, base.LH, base.RANDOM
UPN = "tester@contoso.com"
P, C, CP = "saleslt.SalesLT_Product", "saleslt.SalesLT_Customer", "saleslt.SalesLT_CustomerProfile"
D, H = "saleslt.SalesLT_SalesOrderDetail", "saleslt.SalesLT_SalesOrderHeader"
DUPES = "nexora_test.dim_product_dupes"

# --- fixtures on top of t302/t303: the columns the Net Revenue KPI needs ----------------------
base.LAKEHOUSE["saleslt"]["SalesLT_SalesOrderDetail"].append(col("LineTotal", "DECIMAL(19,4)"))
base.LAKEHOUSE["saleslt"]["SalesLT_SalesOrderHeader"].append(col("Status", "INT"))
base.DATA[("saleslt", "SalesLT_SalesOrderDetail")]["LineTotal"] = [Decimal("10.00")] * 10
base.DATA[("saleslt", "SalesLT_SalesOrderHeader")]["Status"] = [5] * 20

LLM_ANSWER = {
    "entities": [
        {"entity": "Sales", "tables": [D, "saleslt.Ghost_Table"]},
        {"entity": "Product", "tables": [P]},
        {"entity": "Unrelated entity", "tables": [C]},
    ],
    "kpi_fields": [
        {
            "kpi": "Net Revenue",
            "fields": [
                {"need": "line amount", "role": "measure", "candidates": [{"table": D, "column": "LineTotal"}]},
                {
                    "need": "order status",
                    "role": "filter",
                    "candidates": [{"table": H, "column": "NoSuchColumn"}, {"table": H, "column": "Status"}],
                },
                {"need": "invented field", "role": "filter", "candidates": [{"table": "saleslt.Nope", "column": "X"}]},
                {"need": "promotion code", "role": "dimension", "candidates": [{"table": DUPES, "column": "Category"}]},
                {"need": "bad role", "role": "banana", "candidates": []},
            ],
        },
        {"kpi": "Not In The Contract", "fields": []},
    ],
}


class FakeSourceLlm:
    def __init__(self, answer=None):
        self.answer = LLM_ANSWER if answer is None else answer
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, system, user, schema):
        self.calls.append((system, user, schema))
        return self.answer if isinstance(self.answer, str) else json.dumps(self.answer)


class FailingLakehouse(base.FakeLakehouse):
    pass


def write_roles(path: Path) -> None:
    path.write_text(f"roles:\n  data_engineer: [{UPN}]\n  bi_engineer: [{UPN}]\n", encoding="utf-8")


def write_conformed(path: Path, names: list[str]) -> None:
    path.write_text("conformed:\n" + "".join(f"  - {n}\n" for n in names), encoding="utf-8")


def inventory_of(*names: str) -> MetadataInventory:
    tables = []
    for name in names:
        schema, table = name.split(".")
        tables.append(InventoryTable(schema=schema, table_name=table, columns=[InventoryColumn(name="Id", data_type="int")]))
    return MetadataInventory(
        run_id="r", version=1, harvested_at="t", scope=InventoryScope(workspace_id="w", lakehouse_id="l"), tables=tables
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        # --- glossary.yaml ---
        path = tmp_dir / "glossary.yaml"
        try:
            load_glossary(path)
            named = False
        except ConfigError as exc:
            named = "glossary.yaml" in str(exc)
        check("missing glossary.yaml raises ConfigError naming the file", named)
        cases = {
            "not a mapping": ("- a\n", None),
            "no terms": ("other: 1\n", None),
            "words not a list": ("terms:\n  product: [a]\n  bad: nope\n", 3),
            "empty word": ("terms:\n  product: [a, '']\n", 2),
            "yaml syntax": ("terms:\n  product: [a\n", None),
        }
        for label, (body, line) in cases.items():
            path.write_text(body, encoding="utf-8")
            try:
                load_glossary(path)
                message = ""
            except ConfigError as exc:
                message = str(exc)
            check(f"invalid glossary ({label}) names the file", "glossary.yaml" in message)
            if line:
                check(f"invalid glossary ({label}) names line {line}", f"line {line}" in message)
        shipped = load_glossary()
        check("the shipped glossary maps item to product", "item" in shipped.aliases_for("Items") and "product" in shipped.aliases_for("items"))
        check("an unknown entity is named only by itself", shipped.aliases_for("Warehouse") == ["warehouse"])
        check("a plural entity finds the singular term", "client" in shipped.aliases_for("Customers"))
        check(
            "the old matcher still works from the glossary",
            match_requirements_to_data(
                RequirementSpec(business_objective="x", business_entities=["Products"]),
                {"schema_name": "saleslt", "tables": [{"table_name": "SalesLT_Product", "columns": [{"name": "ProductID", "data_type": "int"}]}]},
            )["summary"]["candidate_matches"] == 1,
        )

        # --- the pure resolver: ties, no candidate, score formula ---
        glossary = Glossary({"widget": ["widget"]})
        contract = bc.BusinessContract(run_id="r", business_objective="x", entities=["Widget", "Gadget"])
        inv = inventory_of("s2.Widget", "s1.Widget", "s3.Widget_Old")
        entities, _ = resolve(contract, inv, glossary, frozenset({"s1.widget", "s2.widget"}), {}, Suggestions())
        widget, gadget = entities
        check("equal scores: conformed first, then table name", widget.selected.table == "s1.Widget" and widget.selected.score == 0.9)
        check("the tied table is rejected with its score and the tie-break", [(r.table, r.score) for r in widget.rejected][0] == ("s2.Widget", 0.9)
              and "tie-break" in widget.rejected[0].reason)
        check("a lower-scoring candidate is rejected as not conformed", widget.rejected[1].table == "s3.Widget_Old" and widget.rejected[1].requires_exception
              and "Not conformed" in widget.rejected[1].reason and widget.rejected[1].score == 0.58)
        check("an entity with no candidate has selected null and a reason", gadget.selected is None and "Gadget" in gadget.reason and gadget.rejected == [])
        again, _ = resolve(contract, inv, glossary, frozenset({"s1.widget", "s2.widget"}), {}, Suggestions())
        check("resolve is deterministic", again == entities)

        # --- the endpoint ---
        runs_root = tmp_dir / "runs"
        store = FileRunStore(runs_root)
        sql = t303.EvidenceSql()
        roles_file, conformed_file, glossary_file = tmp_dir / "roles.yaml", tmp_dir / "conformed.yaml", tmp_dir / "glossary.yaml"
        write_roles(roles_file)
        write_conformed(conformed_file, [P, C, D, H])
        glossary_file.write_text(
            "terms:\n  product: [product, products, sku]\n  customer: [customer, customers]\n  sales: [sales, salesorder, salesorderdetail]\n",
            encoding="utf-8",
        )
        original = (roles_module.CONFIG_PATH, conformed_module.CONFIG_PATH, glossary_module.CONFIG_PATH)
        roles_module.CONFIG_PATH, conformed_module.CONFIG_PATH, glossary_module.CONFIG_PATH = roles_file, conformed_file, glossary_file

        llm = FakeSourceLlm()
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        app.dependency_overrides[authenticate] = lambda: AuthContext(
            principal=Principal(oid="o", upn=UPN, name="T"), token="x"
        )
        app.dependency_overrides[get_connector_factory] = lambda: (lambda scope: FailingLakehouse())
        app.dependency_overrides[get_sql_connector_factory] = lambda: (lambda scope: sql)
        app.dependency_overrides[get_source_llm] = lambda: llm
        client = TestClient(app, raise_server_exceptions=False)

        def envelope(r, status, code):
            return r.status_code == status and r.json()["error"]["code"] == code

        def new_run(entities=("Sales", "Product", "Customer"), schemas=("saleslt",), state=RunState.DATA_PROFILED, harvest=True, profile=True):
            run = store.create("Show net revenue by product and customer")
            run.scope = RunScope(
                workspace_id=WS, lakehouse_id=LH, lakehouse_name="SalesLakehouse", schemas=list(schemas),
                sql_endpoint=SqlEndpoint(server="s.fabric", database="SalesLakehouse"),
            )
            run.state = RunState.REQUIREMENT_APPROVED
            store.save(run)
            run_dir = runs_root / run.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            kpi = bc.Kpi(name="Net Revenue", formula_intent="Sum of line totals excluding cancelled orders", grain="order line", currency="USD")
            bc.save(bc.BusinessContract(run_id=run.run_id, business_objective="Net revenue", entities=list(entities), kpis=[kpi]), run_dir / "BusinessContract.yaml")
            if harvest:
                assert client.post(f"/runs/{run.run_id}/harvest").status_code == 200
                if profile:
                    assert client.post(f"/runs/{run.run_id}/profile").status_code == 200
            run = store.get(run.run_id)
            run.state = state
            store.save(run)
            return run.run_id

        def sources(rid):
            return client.post(f"/runs/{rid}/plan/sources")

        def by_entity(body):
            return {e["entity"]: e for e in body["entities"]}

        # AC1: correct selection
        rid = new_run()
        r = sources(rid)
        body = r.json()
        entities = by_entity(body)
        check("plan/sources: 200 with the SourceResolution shape", r.status_code == 200 and body["version"] == 1 and body["run_id"] == rid and body["schema_version"] == 1)
        check("based_on holds the inventory hash", body["based_on"]["MetadataInventory"] == store.get(rid).artifacts["MetadataInventory"].hash)
        check("Sales maps to SalesOrderDetail", entities["Sales"]["selected"]["table"] == D and entities["Sales"]["selected"]["conformed"] is True)
        check("Product maps to SalesLT_Product, Customer to SalesLT_Customer",
              entities["Product"]["selected"]["table"] == P and entities["Customer"]["selected"]["table"] == C)
        check("every selection has a rationale", all(e["selected"]["rationale"] for e in entities.values()))
        check("rationale: glossary word, conformed, key and coverage", "Glossary match 'salesorderdetail'" in entities["Sales"]["selected"]["rationale"]
              and "conformed" in entities["Sales"]["selected"]["rationale"] and "covers 1 of 2 needed fields" in entities["Sales"]["selected"]["rationale"]
              and "key ProductID unique" in entities["Product"]["selected"]["rationale"])
        check("scores follow the formula", entities["Sales"]["selected"]["score"] == 0.75 and entities["Product"]["selected"]["score"] == 0.9)
        header = next(x for x in entities["Sales"]["rejected"] if x["table"] == H)
        check("SalesOrderHeader is a lower-scoring Sales candidate with a reason", header["score"] == 0.63 and "below" in header["reason"] and header["requires_exception"] is False)
        fields = {f["need"]: f for f in body["kpi_fields"][0]["fields"]}
        check("KPI fields map to concrete columns, the status via SalesOrderHeader",
              (fields["line amount"]["table"], fields["line amount"]["column"], fields["line amount"]["role"]) == (D, "LineTotal", "measure")
              and (fields["order status"]["table"], fields["order status"]["column"], fields["order status"]["role"]) == (H, "Status", "filter"))
        unmapped = {u["need"]: u for u in body["kpi_fields"][0]["unmapped"]}
        check("a hallucinated column and table are discarded, never mapped", "invented field" in unmapped and "No column in the inventory" in unmapped["invented field"]["reason"]
              and not any(f["table"] == "saleslt.Nope" for f in fields.values()))
        check("a need whose table isn't in the inventory is unmapped", "promotion code" in unmapped and "promotion code" not in fields)
        check("a need with an invalid role is dropped", "bad role" not in fields and "bad role" not in unmapped)
        check("a KPI the contract lacks is ignored", [k["kpi"] for k in body["kpi_fields"]] == ["Net Revenue"])
        payload = json.loads(llm.calls[0][1])
        check("the model got names and types only, no data", len(llm.calls) == 1 and base.SECRET_NAME not in llm.calls[0][1]
              and set(payload) == {"contract", "glossary", "tables"} and all(set(c) == {"name", "type"} for t in payload["tables"] for c in t["columns"]))
        check("the files exist and the artifact is registered", (runs_root / rid / "SourceResolution.json").is_file()
              and (runs_root / rid / "resolutions" / "SourceResolution.v1.json").is_file() and store.get(rid).artifacts["SourceResolution"].version == 1)
        check("the run state never changes", store.get(rid).state is RunState.DATA_PROFILED)

        # AC5: deterministic, even if the model answers differently the second time
        first = (runs_root / rid / "SourceResolution.json").read_bytes()
        llm.answer = {"entities": [], "kpi_fields": []}
        r = sources(rid)
        check("the same inputs give an identical file and version", r.status_code == 200 and (runs_root / rid / "SourceResolution.json").read_bytes() == first
              and r.json() == body and store.get(rid).artifacts["SourceResolution"].version == 1)
        check("the model is asked once for the same request", len(llm.calls) == 1)
        llm.answer = LLM_ANSWER

        # AC2: rejections explained, two schemas
        rid2 = new_run(schemas=("saleslt", "nexora_test"))
        body2 = sources(rid2).json()
        product = by_entity(body2)["Product"]
        dupes = next(x for x in product["rejected"] if x["table"] == DUPES)
        check("Product still selects SalesLT_Product", product["selected"]["table"] == P)
        check("dim_product_dupes is rejected: not conformed, key not unique", "Not conformed" in dupes["reason"] and "key ProductID not unique" in dupes["reason"]
              and dupes["requires_exception"] is True and dupes["score"] < product["selected"]["score"])

        # AC3: conformed policy
        write_conformed(conformed_file, [C, D, H])  # Product is no longer conformed
        rid3 = new_run(schemas=("saleslt",), state=RunState.REQUIREMENT_APPROVED)
        run = store.get(rid3)
        run.state = RunState.DATA_PROFILED
        store.save(run)
        body3 = sources(rid3).json()
        product = by_entity(body3)["Product"]
        check("a non-conformed Product is not selected, and says it needs an exception",
              product["selected"] is None and "exception" in product["reason"] and product["rejected"][0]["table"] == P and product["rejected"][0]["requires_exception"] is True)
        check("a field in a non-conformed table is unmapped with the reason", "promotion code" not in {f["need"] for f in body2["kpi_fields"][0]["fields"]}
              and any("not conformed" in u["reason"] for u in body2["kpi_fields"][0]["unmapped"] if u["need"] == "promotion code"))

        rid4 = new_run(state=RunState.REQUIREMENT_APPROVED)
        r = client.post(f"/runs/{rid4}/exceptions", json={"table": P, "rationale": "Approved for the pilot"})
        check("exception recorded", r.status_code == 201)
        exception_id = r.json()["exception_id"]
        run = store.get(rid4)
        run.state = RunState.DATA_PROFILED
        store.save(run)
        product = by_entity(sources(rid4).json())["Product"]
        check("with an approved exception, Product is selected and names the exception",
              product["selected"]["table"] == P and product["selected"]["conformed"] is False and product["selected"]["exception_id"] == exception_id
              and "approved exception" in product["selected"]["rationale"] and product["selected"]["score"] == 0.7)
        write_conformed(conformed_file, [P, C, D, H])

        # AC4: glossary without code
        rid5 = new_run(entities=("Items",))
        item = by_entity(sources(rid5).json())["Items"]
        check("'Items' has no candidate while the glossary lacks the word", item["selected"] is None and "Items" in item["reason"])
        glossary_file.write_text("terms:\n  product: [product, products, item, sku]\n", encoding="utf-8")
        item = by_entity(sources(rid5).json())["Items"]
        check("after adding 'item' to the glossary, Items maps to Product", item["selected"]["table"] == P)
        check("the glossary change made a new version", store.get(rid5).artifacts["SourceResolution"].version == 2
              and (runs_root / rid5 / "resolutions" / "SourceResolution.v1.json").is_file())
        glossary_file.write_text(
            "terms:\n  product: [product, products, sku]\n  customer: [customer, customers]\n  sales: [sales, salesorder, salesorderdetail]\n",
            encoding="utf-8",
        )

        # errors
        check("wrong state is 409 WRONG_STATE", envelope(sources(new_run(state=RunState.REQUIREMENT_APPROVED)), 409, "WRONG_STATE"))
        check("before a harvest is 404 NOT_FOUND", envelope(sources(new_run(harvest=False)), 404, "NOT_FOUND"))
        check("unknown run is 404", envelope(client.post(f"/runs/{RANDOM}/plan/sources"), 404, "RUN_NOT_FOUND"))
        calls = len(llm.calls)
        broken = new_run()
        glossary_file.write_text("terms:\n  product: [a]\n  bad: nope\n", encoding="utf-8")
        r = sources(broken)
        check("a malformed glossary is 500 CONFIG_ERROR naming the file and line, before any model call",
              envelope(r, 500, "CONFIG_ERROR") and "glossary.yaml" in r.json()["error"]["message"] and "line 3" in r.json()["error"]["message"] and len(llm.calls) == calls)
        glossary_file.write_text("terms:\n  product: [product]\n  customer: [customer]\n  sales: [sales, salesorderdetail]\n", encoding="utf-8")
        rid6 = new_run()
        llm.answer = "not json"
        check("unusable model output is 502 LLM_INVALID_OUTPUT with nothing saved", envelope(sources(rid6), 502, "LLM_INVALID_OUTPUT")
              and not (runs_root / rid6 / "SourceResolution.json").exists() and "SourceResolution" not in store.get(rid6).artifacts
              and not (runs_root / rid6 / "source_suggestions.json").exists() and store.get(rid6).state is RunState.DATA_PROFILED)
        llm.answer = LLM_ANSWER
        conformed_file.write_text("conformed: x\n", encoding="utf-8")
        check("an invalid conformed config is 500 CONFIG_ERROR", envelope(sources(rid6), 500, "CONFIG_ERROR"))

        roles_module.CONFIG_PATH, conformed_module.CONFIG_PATH, glossary_module.CONFIG_PATH = original
        app.dependency_overrides.clear()

    print("All checks passed.")


if __name__ == "__main__":
    main()
