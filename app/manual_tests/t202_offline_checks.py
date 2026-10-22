"""Offline checks for T-202 (Requirement Compiler with ambiguity detection).

Run: python -m app.manual_tests.t202_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
The LLM is replaced by a fake, and the API runs in-process against a temporary run folder,
so no Azure or Fabric access is needed. The real model is exercised only by the live steps
in .claude/MANUAL_TESTING.md.
"""

import json
import logging
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app import cli
from app.api.deps import get_llm, get_runs_root, get_store
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.compiler import critical_terms as ct
from app.compiler.requirement_compiler import compile_requirement, llm_schema
from app.contracts import business_contract as bc
from app.graphs.base import run_graph
from app.graphs.compile import build_compile_graph
from app.llm.errors import LlmInvalidOutput, LlmTimeout, PromptRejected
from app.store.file_store import FileRunStore
from app.workflow.compile_stage import compile_run
from app.workflow.run import new_run
from app.workflow.states import RunState

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(("PASS " if condition else "FAIL ") + name + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def draft_for(prompt: str) -> dict:
    """What a well-behaved model returns for the test prompts."""
    kpi = {
        "name": "Net Revenue" if "Net Revenue" in prompt else "Revenue",
        "formula_intent": None,
        "grain": None,
        "filters": [],
        "time_logic": "calendar month",
        "exclusions": [],
        "currency": None,
        "expected_examples": [],
        "rationale": None,
    }
    if "SUM(LineTotal)" in prompt:
        kpi.update(
            formula_intent="SUM of LineTotal excluding cancelled orders",
            grain="order line",
            exclusions=["cancelled orders"],
            currency="USD",
        )
    return {
        "domain": None,
        "business_objective": "Show revenue by month for 2024",
        "grain": None,
        "entities": ["Sales"],
        "kpis": [kpi],
        "dimensions": ["Month"],
        "filters": ["Order year = 2024"],
        "currency": None,
        "exclusions": [],
        "security": [],
        "open_questions": [],
        "assumptions": [],
    }


class FakeLlm:
    def __init__(self, outputs=None):
        self.calls: list[tuple[str, str, dict]] = []
        self.outputs = outputs  # list of raw strings, or None for draft_for(prompt)

    def __call__(self, system: str, user: str, schema: dict) -> str:
        self.calls.append((system, user, schema))
        if self.outputs is not None:
            return self.outputs[min(len(self.calls), len(self.outputs)) - 1]
        return json.dumps(draft_for(user))


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def strict_ok(node) -> bool:
    if isinstance(node, dict):
        if "default" in node or "title" in node:
            return False
        if "properties" in node and (
            node.get("additionalProperties") is not False
            or sorted(node.get("required", [])) != sorted(node["properties"])
        ):
            return False
        return all(strict_ok(v) for v in node.values())
    if isinstance(node, list):
        return all(strict_ok(v) for v in node)
    return True


def main() -> int:
    config = ct.load_critical_terms()

    # ---- deterministic rules ----
    undefined = lambda text: ct.detect_undefined_terms(text, config)  # noqa: E731
    check("undefined Revenue is detected", undefined("Show revenue by month for 2024") == ["Revenue"])
    check("alias turnover is detected", undefined("Turnover per region") == ["Revenue"])
    check("Same-Store Sales is detected", undefined("Report same-store sales by quarter") == ["Same-Store Sales"])
    check("no critical term, nothing detected", undefined("List customers by country") == [])
    check("a term inside another word is not matched", undefined("The marginal cost of shipping") == [])
    for text in (
        "Net Revenue = SUM(LineTotal) excluding cancelled orders",
        "Revenue is defined as the sum of line totals",
        "revenue   means order totals",
        "Please define revenue as net of tax",
        "REVENUE=SUM(LineTotal)",
        "Turnover = SUM(LineTotal)",
    ):
        check(f"definition accepted: {text[:34]}", undefined(text) == [], str(undefined(text)))
    check("Margin undefined while Revenue is defined", undefined("Revenue = SUM(LineTotal); show margin by product") == ["Margin"])

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        missing = tmp_dir / "critical_terms.yaml"
        try:
            ct.load_critical_terms(missing)
            named = False
        except ct.ConfigError as exc:
            named = "critical_terms.yaml" in str(exc)
        check("missing config raises ConfigError naming the file", named)
        for label, body in (
            ("not a mapping", "- a\n- b\n"),
            ("empty terms", "terms: {}\ndefinition_patterns: ['{term} =']\n"),
            ("pattern without {term}", "terms: {Revenue: [revenue]}\ndefinition_patterns: ['is defined']\n"),
            ("broken YAML", "terms: [\n"),
        ):
            missing.write_text(body, encoding="utf-8")
            try:
                ct.load_critical_terms(missing)
                ok = False
            except ct.ConfigError as exc:
                ok = "critical_terms.yaml" in str(exc)
            check(f"invalid config ({label}) raises ConfigError", ok)

    # ---- the LLM schema ----
    schema = llm_schema()
    check("LLM schema is strict (all required, no extra, no defaults)", strict_ok(schema))
    check("LLM schema omits fields the code sets", not ({"run_id", "status", "locked_facts", "version"} & set(schema["properties"])))
    check("LLM schema has kpis and open_questions", {"kpis", "open_questions"} <= set(schema["properties"]))

    # ---- the graph ----
    run = new_run("Show revenue by month for 2024")
    before = run.model_dump()
    fake = FakeLlm()
    graph = build_compile_graph(llm=fake, config=config)
    state = {"run": run, "previous": None, "attempt": 0, "errors": [], "attempts_log": []}
    result = run_graph(graph, state, run_id=run.run_id, stage="compile")
    check("node path ends in apply_locked_facts", result["node_path"] == ["llm_compile", "validate_schema", "apply_critical_terms", "apply_locked_facts"], str(result["node_path"]))
    check("the graph doesn't change the run", run.model_dump() == before)
    check("one LLM call when the output is valid", len(fake.calls) == 1)
    rule = [q for q in result["contract"].open_questions if q.source is bc.QuestionSource.RULE]
    check("Revenue rule question is blocking, source rule", len(rule) == 1 and rule[0].blocking and "Revenue" in rule[0].text)
    check("LLM got the strict schema", fake.calls[0][2] == schema)

    sent = fake.calls[0][1]
    check("only the prompt is sent (no runtime data)", sent == "Business requirement:\n" + run.prompt)
    rich = new_run("Show revenue", "Sales", ["report"], ["Use conformed tables only"])
    fake2 = FakeLlm()
    compile_requirement(rich, None, llm=fake2, config=config)
    sent = fake2.calls[0][1]
    check("subject area, outputs and constraints are sent", all(s in sent for s in ("Subject area: Sales", "Desired outputs: report", "Use conformed tables only")))

    # retry once with the errors
    good = json.dumps(draft_for("x"))
    fake3 = FakeLlm(["not json at all", good])
    result = run_graph(build_compile_graph(llm=fake3, config=config), state, run_id=run.run_id, stage="compile")
    check("invalid then valid: 2 LLM calls", len(fake3.calls) == 2)
    check("retry prompt contains the validation error", "output is not valid JSON" in fake3.calls[1][1] and "output is not valid JSON" not in fake3.calls[0][1])
    check("retry path is llm, validate, llm, validate, rules", result["node_path"] == ["llm_compile", "validate_schema", "llm_compile", "validate_schema", "apply_critical_terms", "apply_locked_facts"], str(result["node_path"]))
    bad_shape = json.dumps({**draft_for("x"), "kpi": []})
    fake4 = FakeLlm([bad_shape, good])
    compile_requirement(run, None, llm=fake4, config=config)
    check("an unknown field in the output triggers the retry", len(fake4.calls) == 2 and "kpi: extra field not permitted" in fake4.calls[1][1])
    dup = draft_for("x")
    dup["kpis"] = dup["kpis"] * 2
    fake5 = FakeLlm([json.dumps(dup), good])
    compile_requirement(run, None, llm=fake5, config=config)
    check("duplicate KPI names trigger the retry", len(fake5.calls) == 2 and "duplicate KPI name" in fake5.calls[1][1])

    fake6 = FakeLlm(["nope", "still nope", good])
    try:
        compile_requirement(run, None, llm=fake6, config=config)
        raised = None
    except LlmInvalidOutput as exc:
        raised = exc
    check("invalid twice raises LlmInvalidOutput after exactly 2 attempts", raised is not None and len(fake6.calls) == 2 and len(raised.attempts) == 2)
    check("the failed attempts keep the raw output", raised is not None and raised.attempts[0]["raw"] == "nope")

    # rule questions can't be removed or skipped
    omit = draft_for("x")
    omit["open_questions"] = [{"text": "How is revenue defined?", "blocking": False, "reason": None}]
    contract = compile_requirement(run, None, llm=FakeLlm([json.dumps(omit)]), config=config)
    revenue_qs = [q for q in contract.open_questions if "revenue defined" in q.text.lower()]
    check("an LLM duplicate of a rule question is dropped; the rule one stays", len(revenue_qs) == 1 and revenue_qs[0].source is bc.QuestionSource.RULE and revenue_qs[0].blocking)
    soft = draft_for("x")
    soft["open_questions"] = [{"text": "Which regions?", "blocking": False, "reason": "scope"}]
    contract = compile_requirement(run, None, llm=FakeLlm([json.dumps(soft)]), config=config)
    llm_q = [q for q in contract.open_questions if q.source is bc.QuestionSource.LLM]
    check("LLM questions are kept, with source llm and an id", len(llm_q) == 1 and llm_q[0].id == "Q1" and llm_q[0].status is bc.QuestionStatus.OPEN)
    no_kpi = draft_for("x")
    no_kpi["kpis"] = []
    defined_run = new_run("Revenue = SUM(LineTotal). Show it by month.")
    contract = compile_requirement(defined_run, None, llm=FakeLlm([json.dumps(no_kpi)]), config=config)
    metric = [q for q in contract.open_questions if q.id == ct.METRIC_QUESTION_ID]
    check("no KPI gives a blocking 'Which metric' question", len(metric) == 1 and metric[0].blocking and "metric" in metric[0].text.lower())
    check("the draft status and domain are set by code", contract.status is bc.ContractStatus.DRAFT and contract.domain == "Sales" and contract.version == 1)
    check("the subject area becomes the domain when the LLM gives none", compile_requirement(new_run("Revenue = 1", "Finance"), None, llm=FakeLlm(), config=config).domain == "Finance")

    tidy = draft_for("x")
    tidy["dimensions"] = ["month", "Month", " product category "]
    tidy["entities"] = ["sales", "Sales"]
    contract = compile_requirement(run, None, llm=FakeLlm([json.dumps(tidy)]), config=config)
    check("dimensions and entities are capitalised and de-duplicated", contract.dimensions == ["Month", "Product category"] and contract.entities == ["Sales"], str(contract.dimensions))
    term_dup = draft_for("x")
    term_dup["open_questions"] = [{"text": "What is the definition of Revenue in this context?", "blocking": True, "reason": None}, {"text": "Which regions are in scope?", "blocking": False, "reason": None}]
    contract = compile_requirement(run, None, llm=FakeLlm([json.dumps(term_dup)]), config=config)
    check("an LLM question about a rule-covered term is dropped, others kept", [q.text for q in contract.open_questions] == ["How is Revenue defined?", "Which regions are in scope?"], str([q.text for q in contract.open_questions]))

    # locked facts and earlier answers carry forward
    first = compile_requirement(run, None, llm=FakeLlm(), config=config)
    answered = first.model_copy(
        update={
            "open_questions": [
                q.model_copy(update={"status": bc.QuestionStatus.ANSWERED, "answer": "SUM(LineTotal)"}) if q.id == "RULE-REVENUE" else q
                for q in first.open_questions
            ],
            "locked_facts": [bc.LockedFact(id="F1", text="Revenue = SUM(LineTotal)", source_question_id="RULE-REVENUE", locked_at="2026-10-16T09:00:00Z")],
        }
    )
    second = compile_requirement(run, answered, llm=FakeLlm(), config=config)
    check("a recompile keeps the locked fact unchanged", second.locked_facts == answered.locked_facts)
    check("an answered rule question is not reopened", not [q for q in second.open_questions if q.id == "RULE-REVENUE" and q.status is bc.QuestionStatus.OPEN] and any(q.id == "RULE-REVENUE" and q.status is bc.QuestionStatus.ANSWERED for q in second.open_questions))
    check("no blocking question is left after the answer", not bc.open_blocking_questions(second))
    check("the version goes up", second.version == answered.version + 1)

    src = Path(__file__).resolve().parents[1] / "graphs"
    code = "".join(p.read_text(encoding="utf-8") for p in src.glob("*.py"))
    check("graph code never calls the store or transitions", not any(w in code for w in (".transition(", ".save(", "RunManifest")))

    # ---- API ----
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        store = FileRunStore(tmp_dir / "runs")
        runs_root = tmp_dir / "runs"
        llm = FakeLlm()
        user = Principal(oid="oid-1", upn="tester@contoso.com", name="Tester")
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        app.dependency_overrides[get_llm] = lambda: llm
        app.dependency_overrides[authenticate] = lambda: AuthContext(principal=user, token="t")
        client = TestClient(app)
        capture = Capture()
        logging.getLogger("app.graphs").addHandler(capture)
        logging.getLogger("app.graphs").setLevel(logging.INFO)
        try:
            # AC1
            run1 = store.create("Show revenue by month for 2024")
            response = client.post(f"/runs/{run1.run_id}/compile")
            body = response.json()
            check("AC1 compile returns 200 with run, contract, blocking_questions", response.status_code == 200 and set(body) == {"run", "contract", "blocking_questions"}, response.text[:200])
            check("AC1 state is NEEDS_CLARIFICATION", body["run"]["state"] == "NEEDS_CLARIFICATION" and body["blocking_questions"] == 1)
            question = body["run"]["open_questions"][0]
            check("AC1 open question is blocking, source rule, about Revenue", question["blocking"] and question["source"] == "rule" and "Revenue" in question["text"])
            check("run detail lists the open question", client.get(f"/runs/{run1.run_id}").json()["open_questions"][0]["id"] == question["id"])
            check("run next_action is Answer open questions", body["run"]["next_action"] == "Answer open questions")
            # AC7
            history = store.get(run1.run_id).history
            check("AC7 exactly one new history entry CREATED -> NEEDS_CLARIFICATION", len(history) == 2 and history[-1].from_ is RunState.CREATED and history[-1].to is RunState.NEEDS_CLARIFICATION and history[-1].actor == "tester@contoso.com")
            check("AC7 the log lists the node path, ending in apply_locked_facts", any(line.endswith("apply_locked_facts") and "node_path=" in line and run1.run_id in line for line in capture.lines), str(capture.lines))
            # AC4
            current = runs_root / run1.run_id / "BusinessContract.yaml"
            check("AC4 the contract file is VALID", cli.main(["contract", "validate", str(current)]) == 0)
            # AC5
            check("AC5 contracts/BusinessContract.v1.yaml exists", (runs_root / run1.run_id / "contracts" / "BusinessContract.v1.yaml").is_file())
            artifact = store.get(run1.run_id).artifacts["BusinessContract"]
            check("the contract is registered in the manifest with its hash", artifact.version == 1 and artifact.status == "draft" and artifact.hash == bc.content_hash(current.read_bytes()))
            again = client.post(f"/runs/{run1.run_id}/compile")
            check("AC5 second compile makes v2 and the state stays", again.status_code == 200 and (runs_root / run1.run_id / "contracts" / "BusinessContract.v2.yaml").is_file() and again.json()["run"]["state"] == "NEEDS_CLARIFICATION")
            check("recompiling with the same state adds no history entry", len(store.get(run1.run_id).history) == 2)
            check("the manifest points at version 2", store.get(run1.run_id).artifacts["BusinessContract"].version == 2)

            # AC2, AC3
            prompt2 = "Show Net Revenue by month for 2024. Net Revenue = SUM(LineTotal) excluding cancelled orders, in USD."
            run2 = store.create(prompt2)
            r2 = client.post(f"/runs/{run2.run_id}/compile").json()
            kpi = r2["contract"]["kpis"][0]
            check("AC2 state CREATED and no blocking question", r2["run"]["state"] == "CREATED" and r2["blocking_questions"] == 0)
            check("AC2 formula_intent mentions LineTotal and the exclusion", "LineTotal" in kpi["formula_intent"] and "cancelled" in kpi["formula_intent"])
            check("AC2 next_action is Compile requirement", r2["run"]["next_action"] == "Compile requirement")
            check("AC2 no history entry was added", len(store.get(run2.run_id).history) == 1)
            run3 = store.create(prompt2)
            r3 = client.post(f"/runs/{run3.run_id}/compile").json()
            shape = lambda r: ([k["name"] for k in r["contract"]["kpis"]], r["contract"]["dimensions"], sorted(q["text"] for q in r["contract"]["open_questions"]))  # noqa: E731
            check("AC3 same KPI names, dimensions and questions on two runs", shape(r2) == shape(r3))

            # NEEDS_CLARIFICATION -> CREATED once nothing blocks
            run4 = store.create("Show revenue by month for 2024")
            client.post(f"/runs/{run4.run_id}/compile")
            stored = bc.load(runs_root / run4.run_id / "BusinessContract.yaml")
            resolved = stored.model_copy(update={
                "open_questions": [q.model_copy(update={"status": bc.QuestionStatus.ANSWERED, "answer": "SUM(LineTotal)"}) for q in stored.open_questions],
                "locked_facts": [bc.LockedFact(id="F1", text="Revenue = SUM(LineTotal)", source_question_id="RULE-REVENUE", locked_at="2026-10-16T09:00:00Z")],
            })
            bc.save(resolved, runs_root / run4.run_id / "BusinessContract.yaml")
            r4 = client.post(f"/runs/{run4.run_id}/compile").json()
            check("answered questions move NEEDS_CLARIFICATION back to CREATED", r4["run"]["state"] == "CREATED" and r4["blocking_questions"] == 0)
            check("that transition is in the history", store.get(run4.run_id).history[-1].to is RunState.CREATED)
            check("the locked fact survived the recompile", r4["contract"]["locked_facts"][0]["id"] == "F1")

            # AC6
            run5 = store.create("Show revenue by month for 2024")
            store.transition(run5.run_id, RunState.REQUIREMENT_APPROVED, "test")
            response = client.post(f"/runs/{run5.run_id}/compile")
            check("AC6 wrong state: 409 WRONG_STATE", response.status_code == 409 and response.json()["error"]["code"] == "WRONG_STATE", response.text[:200])
            check("AC6 nothing was written", not (runs_root / run5.run_id / "contracts").exists() and store.get(run5.run_id).state is RunState.REQUIREMENT_APPROVED)

            # failures
            run6 = store.create("Show revenue by month for 2024")
            llm.outputs = ["garbage", "more garbage"]
            llm.calls.clear()
            response = client.post(f"/runs/{run6.run_id}/compile")
            check("invalid twice: 502 LLM_INVALID_OUTPUT", response.status_code == 502 and response.json()["error"]["code"] == "LLM_INVALID_OUTPUT", response.text[:200])
            check("only 2 LLM calls were made", len(llm.calls) == 2)
            check("state is unchanged", store.get(run6.run_id).state is RunState.CREATED and len(store.get(run6.run_id).history) == 1)
            logs = list((runs_root / run6.run_id / "logs").glob("compile-*.json"))
            saved = json.loads(logs[0].read_text(encoding="utf-8")) if logs else {}
            check("the raw output was saved under logs/compile-<ts>.json", len(logs) == 1 and [a["raw"] for a in saved["attempts"]] == ["garbage", "more garbage"])
            check("no contract file was written on failure", not (runs_root / run6.run_id / "BusinessContract.yaml").exists())

            def raising(error):
                def call(system, user, schema):
                    raise error
                return call

            llm_holder = {"fn": raising(LlmTimeout("The model did not answer within 60 seconds"))}
            app.dependency_overrides[get_llm] = lambda: (lambda s, u, sc: llm_holder["fn"](s, u, sc))
            response = client.post(f"/runs/{run6.run_id}/compile")
            check("LLM timeout: 504 UPSTREAM_TIMEOUT, state unchanged", response.status_code == 504 and response.json()["error"]["code"] == "UPSTREAM_TIMEOUT" and store.get(run6.run_id).state is RunState.CREATED)
            llm_holder["fn"] = raising(PromptRejected("The request was rejected by the content filter."))
            response = client.post(f"/runs/{run6.run_id}/compile")
            check("content filter: 422 PROMPT_REJECTED", response.status_code == 422 and response.json()["error"]["code"] == "PROMPT_REJECTED")
            response = client.post("/runs/00000000-0000-4000-8000-000000000000/compile")
            check("unknown run: 404 RUN_NOT_FOUND", response.status_code == 404 and response.json()["error"]["code"] == "RUN_NOT_FOUND")

            # auth is still required
            app.dependency_overrides.pop(authenticate)
            check("compile needs a token: 401", client.post(f"/runs/{run6.run_id}/compile").status_code in (401, 500))
        finally:
            logging.getLogger("app.graphs").removeHandler(capture)
            app.dependency_overrides.clear()

    # ---- startup config check ----
    import app.api.main as main_module

    original = main_module.load_critical_terms

    def broken():
        raise ct.ConfigError("Configuration file critical_terms.yaml is missing")

    main_module.load_critical_terms = broken
    try:
        try:
            with TestClient(app):
                pass
            stopped = False
        except ct.ConfigError as exc:
            stopped = "critical_terms.yaml" in str(exc)
    finally:
        main_module.load_critical_terms = original
    check("a bad critical_terms.yaml stops the API at startup, naming the file", stopped)
    with TestClient(app) as started:
        check("a valid config starts the API", started.get("/health").status_code == 200)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: " + "; ".join(failures))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
