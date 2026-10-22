"""Spans, metrics and model-call cost (T-704, NFR-007).

`init_from_env` reads `OTEL_EXPORTER` (`eventhouse` | `azuremonitor` | `console`; unset defaults to
`eventhouse` inside a Fabric job or UDF, and to no export (a no-op) in local development),
`NEXORA_EVENTHOUSE_URI`/`NEXORA_EVENTHOUSE_DATABASE` for `eventhouse`, and
`APPLICATIONINSIGHTS_CONNECTION_STRING` for `azuremonitor`. `stage_span` records a span per stage
with its duration, outcome and attempt, `record_node` a child span per graph node, and the `record_*`
functions the metrics:

    nexora.stage.duration (ms), nexora.errors, nexora.retries, nexora.validation.passed (0 or 1),
    nexora.llm.tokens.input, nexora.llm.tokens.output, nexora.llm.cost (USD)

Every span, metric point and log line carries `run_id` when there is one (`context.bind`). Telemetry
never breaks a run: each function swallows its own failures and warns once per process. The providers
are private to this module (not the OpenTelemetry globals), so tests can configure them with in-memory
exporters.
"""

import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from app.config import PROJECT_ROOT, get_optional_setting
from app.observability.context import bind, current_job_id, current_run_id, current_stage
from app.observability.logs import CorrelationFilter, redact

logger = logging.getLogger(__name__)

PRICING_PATH = PROJECT_ROOT / "config" / "model_pricing.yaml"
_warned: set[str] = set()


def warn_once(key: str, message: str) -> None:
    """One warning per process for a given problem (an unreachable exporter, a missing price)."""
    if key not in _warned:
        _warned.add(key)
        logger.warning(message)


class _State:
    tracer: Any = None
    meter_provider: Any = None
    tracer_provider: Any = None
    logger_provider: Any = None
    duration: Any = None
    errors: Any = None
    retries: Any = None
    validation: Any = None
    tokens_in: Any = None
    tokens_out: Any = None
    cost: Any = None


_state = _State()


def enabled() -> bool:
    return _state.tracer is not None


def configure(span_exporter: Any = None, metric_reader: Any = None, *, batch: bool = False) -> None:
    """Build the tracer and meter. `span_exporter` and `metric_reader` are OpenTelemetry SDK objects (a test
    passes in-memory ones). Replaces an earlier configuration."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    resource = Resource.create({"service.name": "nexora-api"})
    tracer_provider = TracerProvider(resource=resource)
    if span_exporter is not None:
        tracer_provider.add_span_processor((BatchSpanProcessor if batch else SimpleSpanProcessor)(span_exporter))
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader] if metric_reader is not None else [])
    meter = meter_provider.get_meter("nexora")
    _state.tracer_provider, _state.meter_provider = tracer_provider, meter_provider
    _state.tracer = tracer_provider.get_tracer("nexora")
    _state.duration = meter.create_histogram("nexora.stage.duration", unit="ms", description="Duration of a stage")
    _state.errors = meter.create_counter("nexora.errors", unit="count", description="Stage errors")
    _state.retries = meter.create_counter("nexora.retries", unit="count", description="Retries and repair attempts")
    _state.validation = meter.create_histogram("nexora.validation.passed", unit="1", description="1 if a validation or gate passed, else 0")
    _state.tokens_in = meter.create_counter("nexora.llm.tokens.input", unit="count", description="Model input tokens")
    _state.tokens_out = meter.create_counter("nexora.llm.tokens.output", unit="count", description="Model output tokens")
    _state.cost = meter.create_counter("nexora.llm.cost", unit="USD", description="Model cost")


def shutdown() -> None:
    """Flush and drop the configuration (the tests, and the end of a process)."""
    app_logger = logging.getLogger("app")
    for handler in [h for h in app_logger.handlers if getattr(h, "_nexora_otel", False)]:
        app_logger.removeHandler(handler)  # so nothing flushes a provider that is gone, at exit
    for provider in (_state.tracer_provider, _state.meter_provider, _state.logger_provider):
        try:
            if provider is not None:
                provider.shutdown()
        except Exception:
            pass
    for name in ("tracer", "tracer_provider", "meter_provider", "logger_provider", "duration", "errors", "retries", "validation", "tokens_in", "tokens_out", "cost"):
        setattr(_state, name, None)


class _EventhouseSpanExporter:
    """Sends finished spans to the `nexora_spans` table (T-704)."""

    def __init__(self, sink: Any):
        self._sink = sink

    def export(self, spans: Any) -> Any:
        from opentelemetry.sdk.trace.export import SpanExportResult

        rows = []
        for span in spans:
            ctx = span.get_span_context()
            start, end = span.start_time, span.end_time
            rows.append(
                {
                    "trace_id": format(ctx.trace_id, "032x") if ctx else None,
                    "span_id": format(ctx.span_id, "016x") if ctx else None,
                    "name": span.name,
                    "start_time": start,
                    "end_time": end,
                    "duration_ms": (end - start) / 1_000_000 if start and end else None,
                    **{k: v for k, v in (span.attributes or {}).items()},
                }
            )
        try:
            self._sink.send("nexora_spans", rows)
            return SpanExportResult.SUCCESS
        except Exception:
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def _make_eventhouse_metric_exporter(sink: Any) -> Any:
    """Builds a `MetricExporter` that sends data points to the `nexora_metrics` table (T-704). A function,
    not a module-level class, so the SDK's `MetricExporter` base is only imported when `eventhouse` is
    actually selected."""
    from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult

    class EventhouseMetricExporter(MetricExporter):
        def __init__(self) -> None:
            super().__init__()
            self._sink = sink

        def export(self, metrics_data: Any, timeout_millis: float = 10_000, **kwargs: Any) -> Any:
            rows = []
            for resource_metrics in metrics_data.resource_metrics:
                for scope_metrics in resource_metrics.scope_metrics:
                    for metric in scope_metrics.metrics:
                        for point in getattr(metric.data, "data_points", []):
                            value = getattr(point, "value", None)
                            if value is None:
                                value = getattr(point, "sum", None)
                            rows.append(
                                {
                                    "name": metric.name,
                                    "value": value,
                                    "time_unix_nano": getattr(point, "time_unix_nano", None),
                                    **dict(getattr(point, "attributes", {}) or {}),
                                }
                            )
            try:
                self._sink.send("nexora_metrics", rows)
                return MetricExportResult.SUCCESS
            except Exception:
                return MetricExportResult.FAILURE

        def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
            pass

        def force_flush(self, timeout_millis: float = 10_000) -> bool:
            return True

    return EventhouseMetricExporter()


class _EventhouseLogHandler(logging.Handler):
    """Sends log records to the `nexora_logs` table (T-704): the same fields `JsonFormatter` writes
    locally, never the formatted message of a model-call log (that one is already `redact`ed by the
    caller before it reaches `extra`)."""

    def __init__(self, sink: Any):
        super().__init__()
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            from datetime import datetime, timezone

            row = {
                "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": record.levelname,
                "run_id": getattr(record, "run_id", None),
                "stage": getattr(record, "stage", None),
                "job_id": getattr(record, "job_id", None),
                "msg": record.getMessage(),
                "logger": record.name,
            }
            self._sink.send("nexora_logs", [row])
        except Exception:
            self.handleError(record)


def init_from_env() -> None:
    """Set up export from the settings. Never raises: a bad setting or an exporter that can't start is a
    warning and the run goes on without export. Unset defaults to `eventhouse` inside a Fabric job or UDF
    (the home workspace's own telemetry sink), and to no export in local development."""
    from app.runtime import RUNTIME

    kind = (get_optional_setting("OTEL_EXPORTER") or "").lower()
    if not kind:
        kind = "eventhouse" if RUNTIME in ("fabric_job", "fabric_udf") else ""
    if not kind:
        return
    try:
        if kind == "console":
            from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter

            configure(ConsoleSpanExporter(), PeriodicExportingMetricReader(ConsoleMetricExporter(), export_interval_millis=30000), batch=True)
        elif kind == "eventhouse":
            uri = get_optional_setting("NEXORA_EVENTHOUSE_URI")
            database = get_optional_setting("NEXORA_EVENTHOUSE_DATABASE")
            if not uri or not database:
                warn_once("eventhouse-conn", "OTEL_EXPORTER=eventhouse needs NEXORA_EVENTHOUSE_URI and NEXORA_EVENTHOUSE_DATABASE: telemetry is off")
                return
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

            from app.connectors.eventhouse import EventhouseSink

            sink = EventhouseSink(uri, database)
            configure(_EventhouseSpanExporter(sink), PeriodicExportingMetricReader(_make_eventhouse_metric_exporter(sink), export_interval_millis=30000), batch=True)
            handler = _EventhouseLogHandler(sink)
            handler.addFilter(CorrelationFilter())
            app_logger = logging.getLogger("app")
            for old in [h for h in app_logger.handlers if getattr(h, "_nexora_otel", False)]:
                app_logger.removeHandler(old)
            handler._nexora_otel = True  # type: ignore[attr-defined]
            app_logger.addHandler(handler)
        elif kind == "azuremonitor":
            connection = get_optional_setting("APPLICATIONINSIGHTS_CONNECTION_STRING")
            if not connection:
                warn_once("otel-conn", "OTEL_EXPORTER=azuremonitor needs APPLICATIONINSIGHTS_CONNECTION_STRING: telemetry is off")
                return
            from azure.monitor.opentelemetry.exporter import AzureMonitorLogExporter, AzureMonitorMetricExporter, AzureMonitorTraceExporter
            from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
            from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

            configure(
                AzureMonitorTraceExporter(connection_string=connection),
                PeriodicExportingMetricReader(AzureMonitorMetricExporter(connection_string=connection), export_interval_millis=30000),
                batch=True,
            )
            provider = LoggerProvider()
            provider.add_log_record_processor(BatchLogRecordProcessor(AzureMonitorLogExporter(connection_string=connection)))
            handler = LoggingHandler(logger_provider=provider)
            handler.addFilter(CorrelationFilter())  # run_id and stage become attributes of the trace
            app_logger = logging.getLogger("app")
            for old in [h for h in app_logger.handlers if getattr(h, "_nexora_otel", False)]:
                app_logger.removeHandler(old)
            handler._nexora_otel = True  # type: ignore[attr-defined]
            app_logger.addHandler(handler)
            _state.logger_provider = provider
        else:
            warn_once("otel-kind", f"OTEL_EXPORTER={kind!r} is not one of console, azuremonitor: telemetry is off")
    except Exception as exc:
        shutdown()
        warn_once("otel-init", f"Telemetry couldn't start ({type(exc).__name__}): the run goes on without it")


# --- spans ------------------------------------------------------------------------------------------------------


def _attrs(**extra: Any) -> dict:
    run_id, stage, job_id = current_run_id(), current_stage(), current_job_id()
    out = {k: v for k, v in {"run_id": run_id, "stage": stage, "job_id": job_id, **extra}.items() if v is not None}
    return out


class StageSpan:
    """Handed to the body of `stage_span`: set the outcome, or add an attribute."""

    def __init__(self, span: Any):
        self.span = span
        self.outcome = "success"

    def set_outcome(self, outcome: str) -> None:
        self.outcome = outcome

    def set(self, key: str, value: Any) -> None:
        try:
            if self.span is not None:
                self.span.set_attribute(key, value)
        except Exception:
            pass


@contextmanager
def stage_span(name: str, run_id: str | None = None, attempt: int = 1, **attributes: Any):
    """A span `stage.<name>` with run_id, stage, attempt and outcome, and the stage duration metric. An
    exception inside is the outcome `error` (and counted) and still propagates. The run and the stage are
    bound for logs and child spans while it is open."""
    started = time.perf_counter()
    handle = StageSpan(None)
    with bind(run_id=run_id, stage=name):
        span_cm = None
        try:
            if enabled():
                span_cm = _state.tracer.start_as_current_span(f"stage.{name}", attributes=_attrs(attempt=attempt, **attributes))
                handle.span = span_cm.__enter__()
        except Exception:
            warn_once("span-start", "Telemetry couldn't start a span: spans are skipped")
            span_cm = None
        failure: BaseException | None = None
        try:
            yield handle
        except BaseException as exc:
            failure = exc
            handle.outcome = "error"
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            try:
                handle.set("outcome", handle.outcome)
                if span_cm is not None:
                    span_cm.__exit__(type(failure) if failure else None, failure, failure.__traceback__ if failure else None)
                if enabled():
                    _state.duration.record(elapsed_ms, _attrs(outcome=handle.outcome))
                    if handle.outcome == "error":
                        _state.errors.add(1, _attrs())
            except Exception:
                warn_once("span-end", "Telemetry couldn't record a stage: it is skipped")


def record_node(stage: str, node: str, attempt: int, start_ns: int, end_ns: int, outcome: str = "success") -> None:
    """A child span `stage.<stage>.node.<node>` for one run of a graph node. From the second run of the
    same node on, it is also a retry."""
    try:
        if not enabled():
            return
        span = _state.tracer.start_span(f"stage.{stage}.node.{node}", start_time=start_ns, attributes=_attrs(stage=stage, attempt=attempt, node=node, outcome=outcome))
        span.end(end_time=end_ns)
        if attempt > 1:
            _state.retries.add(1, _attrs(stage=stage, node=node))
    except Exception:
        warn_once("node-span", "Telemetry couldn't record a graph node: it is skipped")


def record_retry(stage: str, kind: str = "attempt") -> None:
    try:
        if enabled():
            _state.retries.add(1, _attrs(stage=stage, kind=kind))
    except Exception:
        warn_once("retry", "Telemetry couldn't record a retry: it is skipped")


def record_error(stage: str | None = None) -> None:
    try:
        if enabled():
            _state.errors.add(1, _attrs(**({"stage": stage} if stage else {})))
    except Exception:
        warn_once("error", "Telemetry couldn't record an error: it is skipped")


def record_validation(subject: str, passed: bool, run_id: str | None = None) -> None:
    """`nexora.validation.passed` as 1 or 0, for a validation run or a gate (`subject` says which)."""
    try:
        if enabled():
            with bind(run_id=run_id):
                _state.validation.record(1 if passed else 0, _attrs(subject=subject))
    except Exception:
        warn_once("validation", "Telemetry couldn't record a validation: it is skipped")


# --- model calls ------------------------------------------------------------------------------------------------


def price_of(deployment: str, input_tokens: int, output_tokens: int, path: Path | None = None) -> float | None:
    """The cost in USD from `config/model_pricing.yaml` (`<deployment>: {input_per_1k, output_per_1k}`), or None
    (with one warning naming the deployment) if the deployment has no usable price."""
    try:
        table = yaml.safe_load((path or PRICING_PATH).read_text(encoding="utf-8")) or {}
        entry = table.get(deployment) if isinstance(table, dict) else None
        rate_in, rate_out = float(entry["input_per_1k"]), float(entry["output_per_1k"])
    except (OSError, yaml.YAMLError, KeyError, TypeError, ValueError):
        warn_once(f"price:{deployment}", f"No usable price for model deployment {deployment!r} in config/model_pricing.yaml: its cost is not recorded")
        return None
    return input_tokens / 1000 * rate_in + output_tokens / 1000 * rate_out


def record_llm(deployment: str, system: str, user: str, output: str, input_tokens: int | None, output_tokens: int | None) -> None:
    """The metrics of one model call and one log line with only lengths and hashes of what was sent and
    received (never the text)."""
    try:
        cost = None
        attrs = _attrs(deployment=deployment)
        if enabled():
            if input_tokens is not None:
                _state.tokens_in.add(input_tokens, attrs)
            if output_tokens is not None:
                _state.tokens_out.add(output_tokens, attrs)
        if input_tokens is not None and output_tokens is not None:
            cost = price_of(deployment, input_tokens, output_tokens)
            if cost is not None and enabled():
                _state.cost.add(cost, attrs)
        logger.info(
            "model call",
            extra={
                "deployment": deployment, "instructions": redact(system), "input": redact(user), "output": redact(output),
                "input_tokens": input_tokens, "output_tokens": output_tokens, "cost_usd": cost,
            },
        )
    except Exception:
        warn_once("llm", "Telemetry couldn't record a model call: it is skipped")
