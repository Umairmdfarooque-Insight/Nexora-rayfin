"""T-704 offline checks: correlation by run_id, spans per stage and per graph node, the metrics, model-call
cost, the rule that content is never logged, and telemetry that never breaks a run.

Run: python -m app.manual_tests.t704_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
The exporters are in-memory, the model is a fake and the API runs in-process on a temporary folder, so no
Azure or Application Insights is needed. The live steps are in MANUAL_TESTING.md.
"""

import io
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict

import requests
from fastapi.testclient import TestClient
from langgraph.graph import END, StateGraph
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.api.deps import get_llm, get_runs_root, get_store
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.graphs.base import run_graph
from app.llm import structured
from app.manual_tests import t202_offline_checks as t202
from app.manual_tests import t502_offline_checks as t502
from app.observability import http as obs_http
from app.observability import telemetry
from app.observability.context import bind
from app.observability.logs import CorrelationFilter, JsonFormatter, redact
from app.observability.middleware import outcome_for, stage_name
from app.store.file_store import FileRunStore
from app.workflow.gates import GateCheck, GateResult, save_gate_result

check = t502.check  # stops at the first failure
SECRET = "zebra-quartz-prompt"  # a phrase from the prompt that must never reach a log


def points(reader: InMemoryMetricReader, name: str) -> list:
    data = reader.get_metrics_data()
    out = []
    for rm in (data.resource_metrics if data else []):
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    out.extend(m.data.data_points)
    return out


def total(reader, name, **match) -> float:
    return sum(
        (getattr(p, "value", None) if getattr(p, "value", None) is not None else getattr(p, "sum", 0))
        for p in points(reader, name)
        if all(p.attributes.get(k) == v for k, v in match.items())
    )


class Loop(TypedDict):
    n: int


def loop_graph():
    g = StateGraph(Loop)
    g.add_node("work", lambda s: {"n": s["n"] + 1})
    g.set_entry_point("work")
    g.add_conditional_edges("work", lambda s: END if s["n"] >= 3 else "work")
    return g.compile()


def main() -> int:
    spans, reader = InMemorySpanExporter(), InMemoryMetricReader()
    telemetry.configure(spans, reader)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(CorrelationFilter())
    handler.setFormatter(JsonFormatter())
    app_logger = logging.getLogger("app")
    app_logger.addHandler(handler)
    app_logger.setLevel(logging.INFO)

    def lines() -> list[dict]:
        return [json.loads(x) for x in stream.getvalue().splitlines() if x.startswith("{")]

    def named(name):
        return [s for s in spans.get_finished_spans() if s.name == name]

    with tempfile.TemporaryDirectory() as tmp:
        runs_root = Path(tmp) / "runs"
        store = FileRunStore(runs_root)
        llm = t202.FakeLlm()
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        app.dependency_overrides[get_llm] = lambda: llm
        app.dependency_overrides[authenticate] = lambda: AuthContext(principal=Principal(oid="o", upn="t@contoso.com", name="T"), token="x")
        client = TestClient(app, raise_server_exceptions=False)
        prompt = f"Show revenue by month for 2024 using SUM(LineTotal), {SECRET}"
        run = store.create(prompt)
        rid = run.run_id

        # --- 1. a stage: span, node spans, metrics ----------------------------------------------------------
        r = client.post(f"/runs/{rid}/compile")
        check("setup: compile works through the API", r.status_code == 200)
        stage = named("stage.compile")
        check("a span stage.compile with run_id, stage, attempt and outcome", len(stage) == 1
              and dict(stage[0].attributes) == {"run_id": rid, "stage": "compile", "attempt": 1, "http.status_code": 200, "outcome": "success"})
        nodes = [s for s in spans.get_finished_spans() if s.name.startswith("stage.compile.node.")]
        check("a child span per graph node, named stage.compile.node.<node>, under the stage span, with run_id and the attempt",
              {s.name for s in nodes} >= {"stage.compile.node.llm_compile", "stage.compile.node.validate_schema"}
              and all(s.attributes["run_id"] == rid and s.attributes["attempt"] == 1 and s.parent.span_id == stage[0].context.span_id for s in nodes))
        check("every span has a start and an end, and the stage took time (a fast node can be under the clock's tick)", stage[0].end_time > stage[0].start_time and all(s.end_time >= s.start_time for s in nodes))
        check("metric nexora.stage.duration (ms) for the stage, tagged with the run", any(
            p.attributes.get("run_id") == rid and p.attributes.get("stage") == "compile" and p.sum > 0 for p in points(reader, "nexora.stage.duration")))

        # --- 2. correlated logs -----------------------------------------------------------------------------------
        mine = [x for x in lines() if x.get("run_id") == rid]
        request = next(x for x in mine if x["msg"] == "request")
        check("every log line made during the call carries the run_id and the stage",
              mine and all(x.get("stage") for x in mine) and request["method"] == "POST" and request["status"] == 200 and request["duration_ms"] >= 0
              and not [x for x in lines() if "run_id=" in x["msg"] and x.get("run_id") != rid])
        check("log lines are JSON with ts, level, msg", all({"ts", "level", "msg"} <= set(x) for x in lines()))
        check("the graph's node-path line is correlated too", any(x["msg"].startswith("stage=compile") and x["run_id"] == rid for x in lines()))

        # --- 3. nothing breaks outside a run, and reads have no span ------------------------------------------------
        before = len(spans.get_finished_spans())
        check("/health works with no run_id and makes no stage span", client.get("/health").status_code == 200 and len(spans.get_finished_spans()) == before)
        client.get(f"/runs/{rid}")
        check("a read binds the run for the logs but makes no span", len(spans.get_finished_spans()) == before
              and any(x["msg"] == "request" and x["method"] == "GET" and x["run_id"] == rid for x in lines()))
        with telemetry.stage_span("outside") as s:
            pass
        check("work outside a run: a span without run_id, and nothing fails", "run_id" not in named("stage.outside")[0].attributes)

        # --- 4. outcomes -----------------------------------------------------------------------------------------------
        errors_before = total(reader, "nexora.errors")
        r = client.post(f"/runs/{rid}/approvals", json={"artifact": "BusinessContract", "action": "ACCEPT", "expected_hash": "sha256:x"})
        refused = named("stage.approvals")[-1]
        check("a refusal (4xx) is the outcome rejected and isn't counted as an error",
              r.status_code >= 400 and refused.attributes["outcome"] == "rejected" and total(reader, "nexora.errors") == errors_before)
        check("outcome_for", (outcome_for(200), outcome_for(409), outcome_for(500)) == ("success", "rejected", "error"))

        def boom(system, user, schema):
            raise RuntimeError("model exploded")

        other = store.create("Another run").run_id
        app.dependency_overrides[get_llm] = lambda: boom
        r = client.post(f"/runs/{other}/compile")
        failed = [s for s in named("stage.compile") if s.attributes["run_id"] == other][0]
        check("an unexpected error: 500, the outcome error and one error counted", r.status_code == 500 and failed.attributes["outcome"] == "error"
              and total(reader, "nexora.errors", run_id=other) >= 1)
        app.dependency_overrides[get_llm] = lambda: llm

        # --- 5. retries and attempts -------------------------------------------------------------------------------------
        spans.clear()
        with bind(run_id="run-loop"):
            final = run_graph(loop_graph(), {"n": 0}, run_id="run-loop", stage="model.repair")
        work = sorted(named("stage.model.repair.node.work"), key=lambda s: s.start_time)
        check("a node that runs again is a span per run with its attempt number, under stage.model.repair",
              final["n"] == 3 and [s.attributes["attempt"] for s in work] == [1, 2, 3] and all(s.attributes["run_id"] == "run-loop" for s in work))
        check("every re-run counts as a retry", total(reader, "nexora.retries", run_id="run-loop") == 2)
        retry_run = store.create(prompt).run_id
        llm_retry = t202.FakeLlm(outputs=["not json", json.dumps(t202.draft_for(prompt))])
        app.dependency_overrides[get_llm] = lambda: llm_retry
        client.post(f"/runs/{retry_run}/compile")
        attempts = sorted(s.attributes["attempt"] for s in named("stage.compile.node.llm_compile") if s.attributes["run_id"] == retry_run)
        check("a compile retried after unusable output shows llm_compile attempts 1 and 2 and a retry", attempts == [1, 2]
              and total(reader, "nexora.retries", run_id=retry_run) >= 1)
        app.dependency_overrides[get_llm] = lambda: llm

        # --- 6. validation outcomes ----------------------------------------------------------------------------------------
        def gate(passed):
            return GateResult(gate="G9", artifact="X", artifact_hash="sha256:x", passed=passed, checked_at="2026-10-16T00:00:00Z",
                              checks=[GateCheck(id="G9.1", description="d", status="pass" if passed else "fail")])

        save_gate_result(runs_root, rid, gate(True))
        save_gate_result(runs_root, rid, gate(False))
        vals = sorted(p.sum for p in points(reader, "nexora.validation.passed") if p.attributes.get("run_id") == rid and p.attributes.get("subject") == "gate:G9")
        check("a gate result is recorded as nexora.validation.passed 1 or 0", vals == [1])
        histogram = [p for p in points(reader, "nexora.validation.passed") if p.attributes.get("subject") == "gate:G9"][0]
        check("both outcomes are counted (a pass and a fail)", histogram.count == 2 and histogram.min == 0 and histogram.max == 1)

        # --- 7. model calls: tokens, cost, no content --------------------------------------------------------------------------
        pricing = Path(tmp) / "pricing.yaml"
        pricing.write_text("test-deployment:\n  input_per_1k: 0.5\n  output_per_1k: 1.5\n", encoding="utf-8")
        telemetry.PRICING_PATH = pricing
        calls: list[dict] = []

        class FakeResponses:
            def create(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(output_text=f'{{"answer": "{SECRET}-reply"}}', usage=SimpleNamespace(input_tokens=2000, output_tokens=1000))

        structured.client = SimpleNamespace(responses=FakeResponses())
        structured.AZURE_OPENAI_DEPLOYMENT = "test-deployment"
        telemetry.AZURE = None
        with bind(run_id="run-llm"):
            out = structured.ask_structured(f"system {SECRET}", f"user {SECRET}", "n", {})
        check("a model call sends the run_id as a header", calls[0]["extra_headers"] == {"x-nexora-run-id": "run-llm"} and SECRET in out)
        check("tokens in and out are metrics of the run", total(reader, "nexora.llm.tokens.input", run_id="run-llm") == 2000
              and total(reader, "nexora.llm.tokens.output", run_id="run-llm") == 1000)
        check("cost comes from config/model_pricing.yaml: 2k in at 0.5 and 1k out at 1.5 is 2.5 USD", abs(total(reader, "nexora.llm.cost", run_id="run-llm") - 2.5) < 1e-9)
        line = next(x for x in lines() if x["msg"] == "model call" and x.get("run_id") == "run-llm")
        check("the log line has lengths and hashes of what was sent and received, and the token counts",
              line["input"] == redact(f"user {SECRET}") and line["output"]["len"] == len(out) and line["input_tokens"] == 2000 and line["cost_usd"] == 2.5)
        check("redact: the length and a short hash, never the text", redact("abc") == {"len": 3, "sha256": "ba7816bf8f01cfea"} and "abc" not in json.dumps(redact("abc")))

        structured.AZURE_OPENAI_DEPLOYMENT = "unpriced-deployment"
        warnings_before = [x for x in lines() if x["level"] == "WARNING" and "unpriced-deployment" in x["msg"]]
        with bind(run_id="run-llm"):
            structured.ask_structured("s", "u", "n", {})
            structured.ask_structured("s", "u", "n", {})
        warnings = [x for x in lines() if x["level"] == "WARNING" and "unpriced-deployment" in x["msg"]]
        check("a deployment with no price: the cost isn't recorded and one warning names it", len(warnings) == 1 and not warnings_before
              and abs(total(reader, "nexora.llm.cost", run_id="run-llm") - 2.5) < 1e-9 and total(reader, "nexora.llm.tokens.input", run_id="run-llm") == 6000)
        structured.AZURE_OPENAI_DEPLOYMENT = "test-deployment"
        check("an invalid pricing file is a warning, not an error", telemetry.price_of("test-deployment", 1, 1, Path(tmp) / "missing.yaml") is None)

        # --- 8. no content in any log line -----------------------------------------------------------------------------------------
        check("no log line holds the prompt, the model's answer or a phrase from them", SECRET not in stream.getvalue())

        # --- 9. telemetry never breaks a run -----------------------------------------------------------------------------------------
        class Failing(InMemorySpanExporter):
            def export(self, spans_):
                raise ConnectionError("exporter unreachable")

        telemetry.configure(Failing(), InMemoryMetricReader())
        survivor = store.create(prompt).run_id
        llm_ok = t202.FakeLlm()
        app.dependency_overrides[get_llm] = lambda: llm_ok
        r = client.post(f"/runs/{survivor}/compile")
        check("an exporter that fails: the run goes on", r.status_code == 200 and store.get(survivor).state.value in ("CREATED", "NEEDS_CLARIFICATION"))

        class BrokenTracer:
            def start_as_current_span(self, *a, **k):
                raise RuntimeError("tracer broke")

            def start_span(self, *a, **k):
                raise RuntimeError("tracer broke")

        telemetry._state.tracer = BrokenTracer()
        r = client.post(f"/runs/{store.create(prompt).run_id}/compile")
        check("a broken tracer: the stage still runs", r.status_code == 200)
        warns = [x for x in lines() if x["level"] == "WARNING" and "Telemetry couldn't" in x["msg"]]
        check("telemetry problems are warned about once per kind", len(warns) == len({x["msg"] for x in warns}) >= 1)
        telemetry.shutdown()
        with telemetry.stage_span("noop", run_id="r"):
            telemetry.record_llm("d", "s", "u", "o", 1, 1)
            telemetry.record_validation("x", True)
            telemetry.record_retry("noop")
        check("with telemetry off every call is a no-op", not telemetry.enabled())

        # --- 10. settings ------------------------------------------------------------------------------------------------------------
        def init(kind, conn=None):
            for key in ("OTEL_EXPORTER", "APPLICATIONINSIGHTS_CONNECTION_STRING"):
                os.environ.pop(key, None)
            if kind:
                os.environ["OTEL_EXPORTER"] = kind
            if conn:
                os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"] = conn
            telemetry._warned.clear()
            telemetry.shutdown()
            telemetry.init_from_env()

        init(None)
        check("OTEL_EXPORTER unset: no export and no span work", not telemetry.enabled())
        init("console")
        check("OTEL_EXPORTER=console enables telemetry", telemetry.enabled())
        init("azuremonitor")
        check("azuremonitor without a connection string: off, one warning", not telemetry.enabled()
              and any("APPLICATIONINSIGHTS_CONNECTION_STRING" in x["msg"] for x in lines() if x["level"] == "WARNING"))
        init("azuremonitor", "not-a-connection-string")
        check("azuremonitor with a bad connection string: off, never an exception", not telemetry.enabled())
        init("azuremonitor", "InstrumentationKey=00000000-0000-0000-0000-000000000000;IngestionEndpoint=http://127.0.0.1:9/")
        check("azuremonitor with a valid connection string enables telemetry", telemetry.enabled())
        telemetry.shutdown()
        init("nonsense")
        check("an unknown exporter is a warning, telemetry stays off", not telemetry.enabled())
        init(None)

    # --- 11. the pieces -------------------------------------------------------------------------------------------------------------------
    check("stage names come from the path: literal lowercase segments only",
          [stage_name(x) for x in ("compile", "model/validate", "model/bridges/abc123/generate", "contract/kpis/Net%20Revenue", "gates/G1/check", "", None)]
          == ["compile", "model.validate", "model.bridges.generate", "contract.kpis", "gates.check", "run", "run"])

    seen: list[dict] = []

    class Capture(requests.adapters.HTTPAdapter):
        def send(self, request, **kwargs):
            seen.append(dict(request.headers))
            response = requests.Response()
            response.status_code = 200
            response._content = b"{}"
            return response

    obs_http.install()
    obs_http.install()
    session = requests.Session()
    session.mount("http://", Capture())
    with bind(run_id="run-http"):
        session.get("http://example.invalid/a")
    session.get("http://example.invalid/b")
    check("every outbound requests call made for a run carries x-nexora-run-id; calls outside a run don't",
          seen[0].get("x-nexora-run-id") == "run-http" and "x-nexora-run-id" not in seen[1])

    # --- 12. the dispatcher binds run_id/stage itself (T-704's Fabric rework: no HTTP middleware in Fabric) ---
    from app.fabric.dispatch import dispatch as fabric_dispatch
    from app.fabric.job import run_job as fabric_run_job
    from app.store.factory import get_run_store

    fabric_spans, fabric_reader = InMemorySpanExporter(), InMemoryMetricReader()
    telemetry.configure(fabric_spans, fabric_reader)
    fabric_store = get_run_store()  # the real local runs/ folder, like t102_dispatch_offline_checks.py
    principal = {"oid": "o-fab", "upn": "fab@contoso.com", "name": "Fab"}
    fab_run_id = fabric_dispatch("POST", "/runs", {}, {"prompt": prompt}, principal, as_job=False)["body"]["run_id"]
    try:
        r = fabric_dispatch("GET", f"/runs/{fab_run_id}", {}, None, principal, as_job=False)
        check("dispatch(): a GET binds the run for logs but makes no span",
              r["status"] == 200 and not [s for s in fabric_spans.get_finished_spans() if s.name.startswith("stage.")])
        r = fabric_dispatch("POST", f"/runs/{fab_run_id}/made-up-op", {}, {}, principal, as_job=False)
        made_up = [s for s in fabric_spans.get_finished_spans() if s.name == "stage.made-up-op"]
        check("dispatch(): a non-GET per-run call records a span named after the path, with run_id and the outcome from the status",
              r["status"] == 404 and len(made_up) == 1
              and made_up[0].attributes.get("run_id") == fab_run_id and made_up[0].attributes.get("outcome") == "rejected")

        job_id = "11111111-1111-1111-1111-111111111111"
        fabric_run_job(job_id, fab_run_id, "POST /runs/{id}/made-up-job-op", {})
        job_span = [s for s in fabric_spans.get_finished_spans() if s.name == "stage.made-up-job-op"]
        check("run_job(): the job's dispatch call also carries job_id, next to run_id",
              len(job_span) == 1 and job_span[0].attributes.get("run_id") == fab_run_id and job_span[0].attributes.get("job_id") == job_id)
    finally:
        shutil.rmtree(Path("runs") / fab_run_id, ignore_errors=True)

    # --- 13. the eventhouse exporter (T-704, OTEL_EXPORTER=eventhouse) --------------------------------------------
    sent_tables: dict[str, list] = {}

    class FakeSink:
        def send(self, table, rows):
            sent_tables.setdefault(table, []).extend(rows)

    sink = FakeSink()
    eh_spans, eh_reader = InMemorySpanExporter(), InMemoryMetricReader()
    telemetry.configure(eh_spans, eh_reader)
    with telemetry.stage_span("eh-demo", run_id="eh-r1"):
        telemetry.record_validation("eh-check", True, run_id="eh-r1")
    span_exporter = telemetry._EventhouseSpanExporter(sink)
    result = span_exporter.export(eh_spans.get_finished_spans())
    check("the eventhouse span exporter sends one row per span to nexora_spans",
          result == SpanExportResult.SUCCESS and len(sent_tables.get("nexora_spans", [])) == 1
          and sent_tables["nexora_spans"][0]["run_id"] == "eh-r1")
    metric_exporter = telemetry._make_eventhouse_metric_exporter(sink)
    from opentelemetry.sdk.metrics.export import MetricExportResult

    metric_result = metric_exporter.export(eh_reader.get_metrics_data())
    check("the eventhouse metric exporter sends data points to nexora_metrics",
          metric_result == MetricExportResult.SUCCESS and len(sent_tables.get("nexora_metrics", [])) > 0)
    log_handler = telemetry._EventhouseLogHandler(sink)
    record = logging.LogRecord(name="app.test", level=logging.INFO, pathname="", lineno=0, msg="hello", args=(), exc_info=None)
    record.run_id, record.stage, record.job_id = "eh-r2", "eh-s", "eh-j"
    log_handler.emit(record)
    check("the eventhouse log handler sends run_id/stage/job_id to nexora_logs, never the message's content unredacted otherwise",
          sent_tables["nexora_logs"][-1]["run_id"] == "eh-r2" and sent_tables["nexora_logs"][-1]["job_id"] == "eh-j")

    init(None)
    check("OTEL_EXPORTER unset locally: still no export (eventhouse only defaults inside a Fabric job or UDF)", not telemetry.enabled())
    init("eventhouse")
    check("OTEL_EXPORTER=eventhouse without NEXORA_EVENTHOUSE_URI/DATABASE: off, one warning",
          not telemetry.enabled() and any("NEXORA_EVENTHOUSE_URI" in x["msg"] for x in lines() if x["level"] == "WARNING"))
    os.environ["NEXORA_EVENTHOUSE_URI"] = "https://example.invalid/ingest"
    os.environ["NEXORA_EVENTHOUSE_DATABASE"] = "nexora_telemetry"
    telemetry.shutdown()
    telemetry.init_from_env()
    check("OTEL_EXPORTER=eventhouse with both settings present enables telemetry", telemetry.enabled())
    telemetry.shutdown()
    for key in ("OTEL_EXPORTER", "NEXORA_EVENTHOUSE_URI", "NEXORA_EVENTHOUSE_DATABASE"):
        os.environ.pop(key, None)

    app_logger.removeHandler(handler)
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
