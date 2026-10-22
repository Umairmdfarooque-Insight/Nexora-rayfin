"""T-502 offline checks: the TMDL generator, `POST /runs/{id}/model/generate` and `cli model generate`.

Run: python -m app.manual_tests.t502_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
The run, its inventory and its approved plan are written by hand into a temporary folder, so no
sign-in, network or LLM is needed. Deploying the files to Fabric is T-501's live test.
"""

import contextlib
import filecmp
import io
import json
import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import app.cli as cli
from app.api.deps import get_runs_root, get_store
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.contracts.metadata_inventory import (
    InventoryColumn,
    InventoryScope,
    InventoryTable,
    MetadataInventory,
)
from app.contracts.model_plan import (
    ExecutionPlan,
    Hierarchy,
    HierarchyLevel,
    Measure,
    ModelColumn,
    ModelSpec,
    ModelTable,
    PlanSource,
    Relationship,
    RelationshipEnd,
    TableSource,
    save_yaml,
    to_data,
)
from app.model.tmdl_generator import ModelGenerationError, generate_tmdl, quote
from app.store.file_store import FileRunStore
from app.workflow.harvest_stage import canonical_hash, write_json
from app.workflow.model_stage import TMDL_ARTIFACT, folder_hash
from app.workflow.run import ArtifactRef, RunScope, SqlEndpoint, utc_now
from app.workflow.states import RunState

WS = "11111111-1111-4111-8111-111111111111"
LH = "22222222-2222-4222-8222-222222222222"
ENDPOINT = SqlEndpoint(server="abc.datawarehouse.fabric.microsoft.com", database="SalesLakehouse")


def check(name: str, condition: bool) -> None:
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        raise SystemExit(1)


def col(name, data_type="string", **kw):
    return ModelColumn(name=name, source_column=kw.pop("source_column", name), data_type=data_type, **kw)


def build_spec(run_id="run-1", storage="DirectLake", dax=False) -> ModelSpec:
    return ModelSpec(
        run_id=run_id,
        model_name="Sales Model",
        storage_mode=storage,
        tables=[
            ModelTable(
                name="Sales",
                source=TableSource(schema="saleslt", table="SalesLT_SalesOrderDetail"),
                role="fact",
                grain="order line",
                columns=[col("SalesOrderID", "int64", is_hidden=True), col("ProductID", "int64", is_hidden=True), col("LineTotal", "decimal", format_string="#,0.00")],
            ),
            ModelTable(
                name="Sales Order",
                source=TableSource(schema="saleslt", table="SalesLT_SalesOrderHeader"),
                role="dimension",
                columns=[col("SalesOrderID", "int64"), col("OrderDate", "dateTime")],
            ),
            ModelTable(
                name="Product",
                source=TableSource(schema="saleslt", table="SalesLT_Product"),
                role="dimension",
                columns=[col("ProductID", "int64"), col("Product Name", "string", source_column="Name", display_folder="Names")],
            ),
            ModelTable(
                name="Date",
                source=TableSource(schema="nexora_test", table="dim_date"),
                role="date",
                columns=[col("Date", "dateTime"), col("Year", "int64"), col("Quarter", "int64"), col("Month", "int64")],
                hierarchies=[Hierarchy(name="Calendar", levels=[HierarchyLevel(name="Year", column="Year"), HierarchyLevel(name="Quarter", column="Quarter"), HierarchyLevel(name="Month", column="Month")])],
            ),
        ],
        relationships=[
            Relationship(name="Sales_Product", **{"from": RelationshipEnd(table="Sales", column="ProductID")}, to=RelationshipEnd(table="Product", column="ProductID"), cardinality="many_to_one", override_rationale="test"),
            Relationship(name="Sales_Order", **{"from": RelationshipEnd(table="Sales", column="SalesOrderID")}, to=RelationshipEnd(table="Sales Order", column="SalesOrderID"), cardinality="many_to_one", cross_filter="both", override_rationale="test"),
            Relationship(name="Order_Date", **{"from": RelationshipEnd(table="Sales Order", column="OrderDate")}, to=RelationshipEnd(table="Date", column="Date"), cardinality="many_to_one", override_rationale="test"),
        ],
        measures=[
            Measure(
                name="Net Revenue", table="Sales", kpi="Net Revenue", format_string="$#,0.00", display_folder="Revenue",
                description="Approved: sum of line totals",
                dax="SUM(Sales[LineTotal])" if dax else None,
            ),
            Measure(name="Order Count", table="Sales", kpi="Net Revenue", dax="DISTINCTCOUNT(Sales[SalesOrderID])\n    + 0" if dax else None),
        ],
    )


def build_plan(spec: ModelSpec) -> ExecutionPlan:
    return ExecutionPlan(
        run_id=spec.run_id,
        based_on={"ModelSpec": canonical_hash(to_data(spec))},
        sources=[PlanSource(table=f"{t.source.schema_name}.{t.source.table}", role=t.role, conformed=True) for t in spec.tables],
    )


def build_inventory(spec: ModelSpec) -> MetadataInventory:
    tables = []
    for t in spec.tables:
        tables.append(
            InventoryTable(
                schema=t.source.schema_name,
                table_name=t.source.table,
                columns=[InventoryColumn(name=c.source_column, data_type=c.data_type) for c in t.columns],
            )
        )
    return MetadataInventory(
        run_id=spec.run_id, version=1, harvested_at=utc_now(),
        scope=InventoryScope(workspace_id=WS, lakehouse_id=LH), tables=tables,
    )


def read_tree(folder: Path) -> dict[str, bytes]:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def run_cli(*argv):
    out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    err = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
        out.flush()
        err.flush()
    return code, out.buffer.getvalue().decode("utf-8"), err.buffer.getvalue().decode("utf-8")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        # --- the generator on its own -------------------------------------------------------
        spec = build_spec(dax=True)
        out = tmp_dir / "gen"
        written = generate_tmdl(spec, out, ENDPOINT)
        tree = read_tree(out)
        check("layout: pbism, database, model, relationships, expressions and one file per table",
              sorted(tree) == sorted([
                  "definition.pbism", "definition/database.tmdl", "definition/model.tmdl", "definition/relationships.tmdl",
                  "definition/expressions.tmdl", "definition/tables/Date.tmdl", "definition/tables/Product.tmdl",
                  "definition/tables/Sales Order.tmdl", "definition/tables/Sales.tmdl"]))
        check("returns the written paths", sorted(p.relative_to(out).as_posix() for p in written) == sorted(tree))
        check("LF line endings and no CRLF anywhere", all(b"\r" not in data for data in tree.values()))
        check("tab indentation, never spaces", all(not line.startswith(" ") for name, data in tree.items() if name.endswith(".tmdl") for line in data.decode().splitlines() if line.strip() and not line.startswith("///") and "DISTINCTCOUNT" not in line))
        text = {k: v.decode("utf-8") for k, v in tree.items()}
        check("database name is quoted when needed", text["definition/database.tmdl"].startswith("database 'Sales Model'\n"))
        check("model refs every table and the expression, sorted",
              "ref table Date\nref table Product\nref table Sales\nref table 'Sales Order'\nref expression DatabaseQuery" in text["definition/model.tmdl"])
        sales = text["definition/tables/Sales.tmdl"]
        check("Direct Lake partition on the entity", "partition Sales = entity\n\t\tmode: directLake\n\t\tsource\n\t\t\tentityName: SalesLT_SalesOrderDetail\n\t\t\tschemaName: saleslt\n\t\t\texpressionSource: DatabaseQuery" in sales)
        check("columns: type, format, hidden, summarizeBy none, sourceColumn",
              "\tcolumn LineTotal\n\t\tdataType: decimal\n\t\tformatString: #,0.00\n" in sales
              and "\tcolumn ProductID\n\t\tdataType: int64\n\t\tisHidden\n" in sales
              and "\t\tsummarizeBy: none\n\t\tsourceColumn: LineTotal" in sales)
        check("measure with a description, folder and format", "\t/// Approved: sum of line totals\n\tmeasure 'Net Revenue' = SUM(Sales[LineTotal])\n\t\tformatString: $#,0.00\n\t\tdisplayFolder: Revenue\n" in sales)
        check("a multi-line measure keeps its lines", "\tmeasure 'Order Count' =\n\t\t\tDISTINCTCOUNT(Sales[SalesOrderID])\n\t\t\t    + 0\n" in sales)
        check("display folder on a column and a renamed source column", "\tcolumn 'Product Name'\n\t\tdataType: string\n\t\tdisplayFolder: Names" in text["definition/tables/Product.tmdl"]
              and "sourceColumn: Name" in text["definition/tables/Product.tmdl"])
        date = text["definition/tables/Date.tmdl"]
        check("Calendar hierarchy with levels in drill order",
              "\thierarchy Calendar\n" in date and date.index("level Year") < date.index("level Quarter") < date.index("level Month")
              and "\t\t\tcolumn: Year" in date)
        rel = text["definition/relationships.tmdl"]
        check("relationships: columns quoted, cardinality and direction", "fromColumn: 'Sales Order'.OrderDate\n\ttoColumn: Date.Date" in rel
              and "crossFilteringBehavior: bothDirections" in rel and rel.count("relationship ") == 3 and "fromCardinality" not in rel)
        check("expression reads the SQL endpoint", 'Sql.Database("abc.datawarehouse.fabric.microsoft.com", "SalesLakehouse")' in text["definition/expressions.tmdl"])
        check("lineageTags are uuids derived from the run and path", "lineageTag: " in sales and all(len(line.split(": ")[1]) == 36 for line in sales.splitlines() if "lineageTag" in line))

        # --- null DAX is left out -----------------------------------------------------------
        plain = tmp_dir / "plain"
        generate_tmdl(build_spec(dax=False), plain, ENDPOINT)
        check("measures whose dax is null are left out", "measure" not in (plain / "definition/tables/Sales.tmdl").read_text(encoding="utf-8"))

        # --- determinism --------------------------------------------------------------------
        again = tmp_dir / "again"
        generate_tmdl(build_spec(dax=True), again, ENDPOINT)
        check("byte-identical for the same spec", read_tree(again) == tree)
        shuffled = build_spec(dax=True)
        shuffled.tables.reverse()
        shuffled.relationships.reverse()
        shuffled.measures.reverse()
        for t in shuffled.tables:
            t.columns.reverse()
        other = tmp_dir / "shuffled"
        generate_tmdl(shuffled, other, ENDPOINT)
        check("byte-identical when objects are listed in another order", read_tree(other) == tree)
        changed = tmp_dir / "other-run"
        generate_tmdl(build_spec(run_id="run-2", dax=True), changed, ENDPOINT)
        check("another run gets other lineageTags", read_tree(changed)["definition/tables/Sales.tmdl"] != tree["definition/tables/Sales.tmdl"])

        # --- Import ------------------------------------------------------------------------
        imp = tmp_dir / "import"
        generate_tmdl(build_spec(storage="Import"), imp, ENDPOINT)
        product = (imp / "definition/tables/Product.tmdl").read_text(encoding="utf-8")
        check("Import writes m partitions instead of Direct Lake", "partition Product = m\n\t\tmode: import" in product
              and 'Source{[Schema="saleslt", Item="SalesLT_Product"]}[Data]' in product and "directLake" not in product)

        # --- names and refusals --------------------------------------------------------------
        check("quote: plain, spaces and a quote", (quote("Sales"), quote("Sales Order"), quote("O'Brien")) == ("Sales", "'Sales Order'", "'O''Brien'"))
        weird = build_spec()
        weird.tables[1].name = "Cust'omer / Régions"
        weird.relationships[1].to.table = "Cust'omer / Régions"
        weird.relationships[2].from_.table = "Cust'omer / Régions"
        wd = tmp_dir / "weird"
        generate_tmdl(weird, wd, ENDPOINT)
        names = [p.name for p in (wd / "definition/tables").iterdir()]
        check("special characters: quoted in TMDL, safe in the file name", "Cust'omer _ Régions.tmdl" in names
              and "table 'Cust''omer / Régions'" in (wd / "definition/tables/Cust'omer _ Régions.tmdl").read_text(encoding="utf-8")
              and "'Cust''omer / Régions'.SalesOrderID" in (wd / "definition/relationships.tmdl").read_text(encoding="utf-8"))

        def refuses(fn):
            try:
                fn()
            except (ModelGenerationError, FileExistsError):
                return True
            return False

        calc = build_spec()
        calc.tables[0].source = None
        check("a table without a source is refused", refuses(lambda: generate_tmdl(calc, tmp_dir / "calc", ENDPOINT)) and not (tmp_dir / "calc").exists())
        bad_rel = build_spec()
        bad_rel.relationships[0].to.column = "NoSuchColumn"
        check("a relationship to a missing column is refused and nothing is written", refuses(lambda: generate_tmdl(bad_rel, tmp_dir / "badrel", ENDPOINT)) and not (tmp_dir / "badrel").exists())
        check("no SQL endpoint is refused", refuses(lambda: generate_tmdl(build_spec(), tmp_dir / "noep", None)))
        check("an existing non-empty folder is never overwritten", refuses(lambda: generate_tmdl(build_spec(), out, ENDPOINT)) and read_tree(out) == tree)
        newline = build_spec()
        newline.tables[0].name = "Bad\nName"
        check("a name with a line break is refused", refuses(lambda: generate_tmdl(newline, tmp_dir / "nl", ENDPOINT)))

        # --- the run service, endpoint and CLI -----------------------------------------------
        runs_root = tmp_dir / "runs"
        store = FileRunStore(runs_root)
        user = {"upn": "tester@contoso.com"}
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        app.dependency_overrides[authenticate] = lambda: AuthContext(principal=Principal(oid="o", upn=user["upn"], name="T"), token="x")
        client = TestClient(app, raise_server_exceptions=False)
        original_store = cli.get_run_store
        cli.get_run_store = lambda: store

        def envelope(r, status, code):
            return r.status_code == status and r.json()["error"]["code"] == code

        def new_run(state=RunState.PLAN_APPROVED, approved=True, endpoint=ENDPOINT, mutate=None, scope=True):
            run = store.create("Show net revenue by product")
            run_spec = build_spec(run_id=run.run_id)
            if mutate:
                mutate(run_spec)
            run_dir = runs_root / run.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            plan = build_plan(run_spec)
            save_yaml(run_spec, run_dir / "ModelSpec.yaml")
            save_yaml(plan, run_dir / "ExecutionPlan.yaml")
            write_json(run_dir / "MetadataInventory.json", build_inventory(run_spec).to_dict())
            if scope:
                run.scope = RunScope(workspace_id=WS, lakehouse_id=LH, lakehouse_name="SalesLakehouse", sql_endpoint=endpoint)
            status = "approved" if approved else "draft"
            run.artifacts["ModelSpec"] = ArtifactRef(path="ModelSpec.yaml", version=1, hash=canonical_hash(to_data(run_spec)), status=status, updated_at=utc_now())
            run.artifacts["ExecutionPlan"] = ArtifactRef(path="ExecutionPlan.yaml", version=1, hash=canonical_hash(to_data(plan)), status=status, updated_at=utc_now())
            run.state = state
            store.save(run)
            return run.run_id

        run_id = new_run()
        r = client.post(f"/runs/{run_id}/model/generate")
        body = r.json()
        check("POST generate: 200 with version, files and the run", r.status_code == 200 and body["version"] == 1
              and "definition/tables/Sales.tmdl" in body["files"] and body["run"]["state"] == "MODEL_GENERATING")
        v1 = runs_root / run_id / "model" / "v1"
        check("v1 has a table file per ModelSpec table", sorted(p.stem for p in (v1 / "definition/tables").iterdir()) == ["Date", "Product", "Sales", "Sales Order"])
        ref = store.get(run_id).artifacts[TMDL_ARTIFACT]
        check("TMDLModel artifact registered", ref.path == "model/v1" and ref.version == 1 and ref.hash == folder_hash(v1) and ref.status == "draft"
              and ref.based_on["ModelSpec"] == store.get(run_id).artifacts["ModelSpec"].hash)

        again_response = client.post(f"/runs/{run_id}/model/generate")
        check("generating in MODEL_GENERATING is 409 WRONG_STATE", envelope(again_response, 409, "WRONG_STATE") and not (runs_root / run_id / "model" / "v2").exists())
        check("wrong state: a plan that isn't approved yet", envelope(client.post(f"/runs/{new_run(state=RunState.DATA_PROFILED)}/model/generate"), 409, "WRONG_STATE"))
        check("an unapproved ModelSpec is 404 NOT_FOUND", envelope(client.post(f"/runs/{new_run(approved=False)}/model/generate"), 404, "NOT_FOUND"))
        check("a run without a scope is 400", envelope(client.post(f"/runs/{new_run(scope=False)}/model/generate"), 400, "BAD_REQUEST"))
        check("a scope without a SQL endpoint is 400", envelope(client.post(f"/runs/{new_run(endpoint=None)}/model/generate"), 400, "BAD_REQUEST"))
        check("unknown run is 404", envelope(client.post("/runs/nope/model/generate"), 404, "RUN_NOT_FOUND"))

        def break_reference(s):
            s.relationships[0].to.column = "NoSuchColumn"

        broken = new_run(mutate=break_reference)
        r = client.post(f"/runs/{broken}/model/generate")
        check("an invalid ModelSpec is 422 with the validation errors and nothing written",
              envelope(r, 422, "VALIDATION_ERROR") and "PLAN.1" in r.json()["error"]["message"] and "Product.NoSuchColumn" in r.json()["error"]["message"]
              and not (runs_root / broken / "model").exists() and store.get(broken).state is RunState.PLAN_APPROVED)

        edited = new_run()
        spec_path = runs_root / edited / "ModelSpec.yaml"
        spec_path.write_bytes(spec_path.read_bytes().replace(b"model_name: Sales Model", b"model_name: Changed"))
        check("a ModelSpec edited after approval is 409 STALE_VERSION", envelope(client.post(f"/runs/{edited}/model/generate"), 409, "STALE_VERSION"))

        failed = new_run(state=RunState.MODEL_FAILED)
        check("MODEL_FAILED is refused for a normal call", envelope(client.post(f"/runs/{failed}/model/generate"), 409, "WRONG_STATE"))
        from app.workflow.model_stage import generate_model

        version, files, run_after = generate_model(store, runs_root, failed, from_repair=True)
        check("repair may generate from MODEL_FAILED", version == 1 and run_after.state is RunState.MODEL_GENERATING)

        # CLI: the next version, and no difference to v1
        code, out_text, err_text = run_cli("model", "generate", str(runs_root / run_id))
        check("cli: writes v2 and lists its files", code == 0 and out_text.startswith("v2: ") and "definition/tables/Sales.tmdl" in out_text)
        v2 = runs_root / run_id / "model" / "v2"
        check("cli: v2 is identical to v1", read_tree(v1) == read_tree(v2) and not filecmp.dircmp(v1, v2).diff_files)
        check("cli: v1 was not modified", folder_hash(v1) == ref.hash)
        check("cli: the state is untouched", store.get(run_id).state is RunState.MODEL_GENERATING and store.get(run_id).artifacts[TMDL_ARTIFACT].version == 2)
        code, _, _ = run_cli("model", "generate", str(runs_root / "missing"))
        check("cli: unknown folder is exit 2", code == 2)
        code, out_text, _ = run_cli("model", "generate", str(runs_root / broken))
        check("cli: an invalid ModelSpec prints INVALID with the path, exit 1", code == 1 and "INVALID" in out_text and "Product.NoSuchColumn" in out_text)
        code, _, err_text = run_cli("model", "generate", str(runs_root / new_run(approved=False)))
        check("cli: an unapproved plan is exit 1", code == 1 and "approved" in err_text)

        # No LLM: nothing in the generation path may reach one
        saved = {k: os.environ.get(k) for k in ("AZURE_OPENAI_ENDPOINT",)}
        os.environ["AZURE_OPENAI_ENDPOINT"] = "https://invalid.invalid"
        try:
            check("no LLM: generation still succeeds with an invalid endpoint", client.post(f"/runs/{new_run()}/model/generate").status_code == 200)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        cli.get_run_store = original_store
        app.dependency_overrides.clear()

    print("All checks passed.")


if __name__ == "__main__":
    main()
