"""Traceability of a visual (T-703, FR-022): visual -> measure -> DAX -> approved KPI definition -> source
columns with their profile evidence -> the original prompt.

Everything is read from files the run keeps. A version file that is missing (the contract version that
was approved, the model folder, the inventory) is reported in `gaps` instead of failing or being skipped.
"""

from pathlib import Path

from app.contracts import business_contract as bc
from app.contracts.metadata_inventory import MetadataInventory
from app.contracts.model_plan import ModelSpec, load_yaml
from app.contracts.report_plan import FieldRef, ReportPlan
from app.contracts.source_resolution import SourceResolution
from app.model.tmdl_reader import read_model
from app.store.base import RunStore
from app.workflow.approval import Action, list_approvals
from app.workflow.clarification import NotFound
from app.workflow.model_stage import TMDL_ARTIFACT
from app.workflow.plan_stage import MODEL_FILE
from app.workflow.report_plan_stage import CURRENT as REPORT_PLAN_FILE
from app.workflow.source_stage import CURRENT as RESOLUTION_FILE


def _refs(visual) -> list[FieldRef]:
    return [*visual.fields.values, *visual.fields.category, *visual.fields.series]


def trace(store: RunStore, runs_root: Path, run_id: str, visual_id: str) -> dict:
    """`GET /runs/{id}/trace?visual_id=`. Raises NotFound (404) for a run without a report plan or a visual
    the plan doesn't have."""
    run = store.get(run_id)
    run_dir = Path(runs_root) / run_id
    plan_path = run_dir / REPORT_PLAN_FILE
    if not plan_path.is_file():
        raise NotFound("The run has no report plan yet, so there is no visual to trace")
    plan = ReportPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    found = next(((p, v) for p in plan.pages for v in p.visuals if v.id == visual_id), None)
    if found is None:
        raise NotFound(f"The report has no visual {visual_id!r}")
    page, visual = found
    questions = {q.id: q.text for q in plan.questions}
    gaps: list[str] = []

    out: dict = {
        "visual": {
            "id": visual.id, "title": visual.title, "type": visual.type, "page": page.name,
            "questions": [{"id": q, "text": questions.get(q, "")} for q in visual.question_ids],
        },
        "measure": None,
        "other_measures": [],
        "kpi": None,
        "sources": [],
        "prompt": run.prompt,
        "gaps": gaps,
    }
    names = [r.measure for r in _refs(visual) if r.measure]
    if not names:
        gaps.append("The visual binds no measure, so it has no KPI behind it")
        return out
    out["other_measures"] = names[1:]

    # measure and DAX, as deployed
    spec_path = run_dir / MODEL_FILE
    spec = load_yaml(ModelSpec, spec_path) if spec_path.is_file() else None
    measure = next((m for m in (spec.measures if spec else []) if m.name == names[0]), None)
    tmdl = run.artifacts.get(TMDL_ARTIFACT)
    dax = measure.dax if measure else None
    if tmdl is not None:
        folder = run_dir / tmdl.path
        if folder.is_dir():
            table = read_model(folder).tables.get(measure.table if measure else "")
            dax = (table.measures.get(names[0]) if table else None) or dax
        else:
            gaps.append(f"Model version {tmdl.version} is missing ({tmdl.path})")
    else:
        gaps.append("The run has no generated model yet")
    if measure is None:
        gaps.append(f"The model specification has no measure {names[0]!r}")
    out["measure"] = {"name": names[0], "dax": dax, "model_version": tmdl.version if tmdl else None}
    if measure is None:
        return out

    # the KPI, as the business owner approved it
    approval = next((r for r in reversed(list_approvals(store, runs_root, run_id)) if r.artifact == "BusinessContract" and r.action is Action.ACCEPT), None)
    definition = None
    if approval is None:
        gaps.append("The business contract has no approval record")
    else:
        path = run_dir / "contracts" / f"BusinessContract.v{approval.artifact_version}.yaml"
        try:
            contract = bc.load(path)
            kpi = next((k for k in contract.kpis if k.name.casefold() == measure.kpi.casefold()), None)
            if kpi is None:
                gaps.append(f"KPI {measure.kpi!r} is not in contract version {approval.artifact_version}")
            else:
                definition = kpi.model_dump(mode="json", include={"name", "formula_intent", "grain", "filters", "time_logic", "exclusions", "currency"})
        except (OSError, ValueError, bc.ContractInvalid):
            gaps.append(f"Contract version {approval.artifact_version} is missing or unreadable")
    out["kpi"] = {
        "definition": definition,
        "contract_version": approval.artifact_version if approval else None,
        "approved_by": approval.user.get("upn") if approval else None,
        "approved_at": approval.at if approval else None,
    }

    # the source columns of the KPI, with the profile evidence behind them
    resolution_path = run_dir / RESOLUTION_FILE
    if not resolution_path.is_file():
        gaps.append("Sources haven't been resolved for this run (no SourceResolution)")
        return out
    resolution = SourceResolution.model_validate_json(resolution_path.read_text(encoding="utf-8"))
    entry = next((k for k in resolution.kpi_fields if k.kpi.casefold() == measure.kpi.casefold()), None)
    if entry is None:
        gaps.append(f"The source resolution has no fields for KPI {measure.kpi!r}")
        return out
    inventory_path = run_dir / "MetadataInventory.json"
    inventory = None
    if inventory_path.is_file():
        inventory = MetadataInventory.model_validate_json(inventory_path.read_text(encoding="utf-8"))
    else:
        gaps.append("The metadata inventory is missing, so the sources have no profile evidence")
    for f in entry.fields:
        evidence = None
        table = next((t for t in (inventory.tables if inventory else []) if f"{t.schema_name}.{t.table_name}".casefold() == f.table.casefold()), None)
        if inventory is not None:
            profile = table.profile if table else None
            column = next((c for n, c in (profile.columns.items() if profile else []) if n.casefold() == f.column.casefold()), None)
            if column is None:
                gaps.append(f"No profile evidence for {f.table}.{f.column}")
            else:
                evidence = {
                    **column.model_dump(mode="json", exclude={"note"}), "row_count": profile.row_count,
                    "profiled_at": profile.evidence.profiled_at, "queries": profile.evidence.queries, "note": column.note,
                }
        out["sources"].append({"table": f.table, "column": f.column, "need": f.need, "role": f.role, "profile_evidence": evidence})
    for u in entry.unmapped:
        gaps.append(f"Need {u.need!r} of the KPI has no source: {u.reason}")
    return out
