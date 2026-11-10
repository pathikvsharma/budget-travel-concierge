"""Structured JSON logging, intent-vs-outcome capture, and OpenTelemetry tracing.

Features:
- JSON-structured logs via `python-json-logger` (zero `print()` usage).
- Mandatory PII redaction filter attached to every logger handler.
- OpenTelemetry `TracerProvider` linking turn -> agent -> model -> tool spans,
  with Google Cloud Trace (`CloudTraceSpanExporter`) activation in cloud runs.
- `ObservabilityPlugin` (`BasePlugin`) capturing correlated `tool.intent` prior
  to tool execution and `tool.outcome` / `tool.error` afterward with latency.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any, Optional
import uuid

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Span, Status, StatusCode
from pythonjsonlogger.json import JsonFormatter

from travel_concierge.config import load_settings
from travel_concierge.pii import redact_pii, redact_pii_text

_LOGGER_INITIALIZED = False
_TRACING_INITIALIZED = False

# Tool outcomes that represent a failed or refused action (vs. success or a HITL pause).
_FAILED_TOOL_OUTCOMES = frozenset({"error", "rejected_by_human"})


class InMemoryRingSpanExporter(SpanExporter):
    """Bounded ring-buffer span exporter supporting local diagnostics and tests."""

    def __init__(self, max_spans: int = 500) -> None:
        self._max_spans = max_spans
        self._spans: list[ReadableSpan] = []

    def export(self, spans: Any) -> SpanExportResult:
        for span in spans:
            self._spans.append(span)
        if len(self._spans) > self._max_spans:
            self._spans = self._spans[-self._max_spans :]
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def get_finished_spans(self) -> list[ReadableSpan]:
        return list(self._spans)

    def clear(self) -> None:
        self._spans.clear()


RING_SPAN_EXPORTER = InMemoryRingSpanExporter()


class PiiRedactingLogFilter(logging.Filter):
    """Scrub PII and secret tokens from every structured log record before emit."""

    _SKIP_ATTRS = frozenset(
        {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "taskName",
            "processName",
            "process",
        }
    )

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg, _ = redact_pii_text(record.msg)
        if record.args:
            record.args = redact_pii(record.args)
        for key, val in list(record.__dict__.items()):
            if key not in self._SKIP_ATTRS:
                record.__dict__[key] = redact_pii(val)
        return True


def configure_structured_logging() -> logging.Logger:
    """Configure the structured JSON logger for TripPilot once per process."""
    global _LOGGER_INITIALIZED
    settings = load_settings()
    logger = logging.getLogger("travel_concierge")

    if not _LOGGER_INITIALIZED:
        level = getattr(logging, settings.log_level, logging.INFO)
        logger.setLevel(level)
        logger.propagate = False

        handler = logging.StreamHandler(sys.stderr)
        formatter = JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={
                "asctime": "timestamp",
                "levelname": "severity",
                "name": "logger",
            },
        )
        handler.setFormatter(formatter)
        handler.addFilter(PiiRedactingLogFilter())
        logger.handlers = [handler]
        _LOGGER_INITIALIZED = True

    return logger


def configure_distributed_tracing() -> trace.Tracer:
    """Initialize OpenTelemetry TracerProvider and optional GCP Cloud Trace export."""
    global _TRACING_INITIALIZED
    settings = load_settings()

    if not _TRACING_INITIALIZED:
        resource = Resource.create(
            {
                "service.name": settings.service_name,
                "service.namespace": "trippilot",
                "agent.framework": "google-adk-python",
            }
        )
        current_provider = trace.get_tracer_provider()
        if isinstance(current_provider, TracerProvider):
            provider = current_provider
        else:
            provider = TracerProvider(resource=resource)
            trace.set_tracer_provider(provider)

        provider.add_span_processor(SimpleSpanProcessor(RING_SPAN_EXPORTER))

        if settings.enable_cloud_trace:
            try:
                from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

                cloud_exporter = CloudTraceSpanExporter(project_id=settings.gcp_project)
                provider.add_span_processor(BatchSpanProcessor(cloud_exporter))
                configure_structured_logging().info(
                    "Google Cloud Trace exporter attached",
                    extra={"event_type": "telemetry.cloud_trace_enabled"},
                )
            except Exception as exc:
                configure_structured_logging().warning(
                    "Cloud Trace exporter unavailable; continuing with local OTel spans",
                    extra={
                        "event_type": "telemetry.cloud_trace_fallback",
                        "reason": type(exc).__name__,
                    },
                )
        _TRACING_INITIALIZED = True

    return trace.get_tracer("travel_concierge.telemetry")


def get_logger() -> logging.Logger:
    """Return the configured PII-redacting structured JSON logger."""
    return configure_structured_logging()


def get_tracer() -> trace.Tracer:
    """Return the configured OpenTelemetry tracer."""
    return configure_distributed_tracing()


def _current_trace_identifiers(span: Span | None = None) -> dict[str, str]:
    active_span = span or trace.get_current_span()
    ctx = active_span.get_span_context() if active_span else None
    if ctx and ctx.is_valid:
        return {
            "trace_id": f"{ctx.trace_id:032x}",
            "span_id": f"{ctx.span_id:016x}",
        }
    return {"trace_id": "", "span_id": ""}


def log_structured_event(
    event_type: str,
    message: str,
    *,
    severity: int = logging.INFO,
    span: Span | None = None,
    **metadata: Any,
) -> None:
    """Emit a structured JSON log record enriched with trace IDs and scrubbed data."""
    logger = get_logger()
    extra_fields = {
        "event_type": event_type,
        **_current_trace_identifiers(span),
        **redact_pii(metadata),
    }
    logger.log(severity, message, extra=extra_fields)


def classify_tool_outcome(result: Any) -> str:
    """Normalize a tool result payload into an outcome status for audit logs.

    Understands TripPilot's `status` envelopes as well as ADK's built-in
    `{"error": ...}` responses, such as a pending or rejected HITL confirmation.
    """
    if not isinstance(result, dict):
        return "success"
    if "status" in result:
        return str(result["status"])
    if "error" in result:
        error_text = str(result["error"]).lower()
        if "requires confirmation" in error_text:
            return "awaiting_human_confirmation"
        if "rejected" in error_text:
            return "rejected_by_human"
        return "error"
    return "success"


class ObservabilityPlugin(BasePlugin):
    """ADK Plugin recording Intent-vs-Outcome audit events and distributed spans."""

    def __init__(self, name: str = "trippilot_observability") -> None:
        super().__init__(name=name)
        self._agent_spans: dict[str, tuple[Span, float]] = {}
        self._model_spans: dict[str, tuple[Span, float]] = {}
        self._tool_spans: dict[str, tuple[Span, float, str]] = {}

    @staticmethod
    def _session_id_from_ctx(ctx: Any) -> str:
        session = getattr(ctx, "session", None)
        return str(getattr(session, "id", "")) if session else ""

    @staticmethod
    def _tool_call_key(tool_name: str, tool_context: ToolContext) -> str:
        fc_id = getattr(tool_context, "function_call_id", None) or ""
        inv_id = getattr(tool_context, "invocation_id", None) or ""
        return f"{inv_id}:{tool_name}:{fc_id}"

    async def before_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> Optional[Any]:
        tracer = get_tracer()
        key = f"{callback_context.invocation_id}:{agent.name}"
        span = tracer.start_span(
            f"agent.{agent.name}",
            attributes={
                "agent.name": agent.name,
                "adk.invocation_id": str(callback_context.invocation_id),
                "adk.session_id": self._session_id_from_ctx(callback_context),
            },
        )
        self._agent_spans[key] = (span, time.perf_counter())
        log_structured_event(
            "agent.start",
            f"Starting agent {agent.name}",
            span=span,
            agent_name=agent.name,
            invocation_id=str(callback_context.invocation_id),
            session_id=self._session_id_from_ctx(callback_context),
        )
        return None

    async def after_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> Optional[Any]:
        key = f"{callback_context.invocation_id}:{agent.name}"
        entry = self._agent_spans.pop(key, None)
        span, started = entry if entry else (None, time.perf_counter())
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        log_structured_event(
            "agent.complete",
            f"Completed agent {agent.name}",
            span=span,
            agent_name=agent.name,
            invocation_id=str(callback_context.invocation_id),
            session_id=self._session_id_from_ctx(callback_context),
            latency_ms=latency_ms,
        )
        if span:
            span.set_attribute("agent.latency_ms", latency_ms)
            span.set_status(Status(StatusCode.OK))
            span.end()
        return None

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> Optional[LlmResponse]:
        tracer = get_tracer()
        model_name = str(getattr(llm_request, "model", "") or "default")
        key = f"{callback_context.invocation_id}:{callback_context.agent_name}"
        span = tracer.start_span(
            f"llm.{model_name}",
            attributes={
                "gen_ai.system": "gemini",
                "gen_ai.request.model": model_name,
                "agent.name": callback_context.agent_name,
                "adk.invocation_id": str(callback_context.invocation_id),
            },
        )
        self._model_spans[key] = (span, time.perf_counter())
        log_structured_event(
            "llm.intent",
            "Dispatching vetted prompt to Gemini model",
            span=span,
            agent_name=callback_context.agent_name,
            model=model_name,
            invocation_id=str(callback_context.invocation_id),
            content_turns=len(getattr(llm_request, "contents", []) or []),
        )
        return None

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> Optional[LlmResponse]:
        if getattr(llm_response, "partial", False):
            # Streaming chunk: keep the span open until the final aggregated response.
            return None
        key = f"{callback_context.invocation_id}:{callback_context.agent_name}"
        entry = self._model_spans.pop(key, None)
        span, started = entry if entry else (None, time.perf_counter())
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        log_structured_event(
            "llm.outcome",
            "Received Gemini model response",
            span=span,
            agent_name=callback_context.agent_name,
            invocation_id=str(callback_context.invocation_id),
            latency_ms=latency_ms,
        )
        if span:
            span.set_attribute("gen_ai.latency_ms", latency_ms)
            span.set_status(Status(StatusCode.OK))
            span.end()
        return None

    async def on_model_error_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
        error: Exception,
    ) -> Optional[LlmResponse]:
        """Close the active LLM span and emit a structured model-error event."""
        key = f"{callback_context.invocation_id}:{callback_context.agent_name}"
        entry = self._model_spans.pop(key, None)
        span, started = entry if entry else (None, time.perf_counter())
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        log_structured_event(
            "llm.error",
            f"Gemini model call failed with {type(error).__name__}",
            severity=logging.ERROR,
            span=span,
            agent_name=callback_context.agent_name,
            invocation_id=str(callback_context.invocation_id),
            model=str(getattr(llm_request, "model", "") or "default"),
            error_type=type(error).__name__,
            latency_ms=latency_ms,
        )
        if span:
            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, type(error).__name__))
            span.end()
        return None

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> Optional[dict[str, Any]]:
        """Record the agent's intended tool invocation prior to execution."""
        tracer = get_tracer()
        correlation_id = uuid.uuid4().hex[:12]
        safe_args = redact_pii(tool_args)
        span = tracer.start_span(
            f"tool.{tool.name}",
            attributes={
                "tool.name": tool.name,
                "agent.name": str(tool_context.agent_name),
                "tool.correlation_id": correlation_id,
                "adk.invocation_id": str(tool_context.invocation_id),
            },
        )
        key = self._tool_call_key(tool.name, tool_context)
        self._tool_spans[key] = (span, time.perf_counter(), correlation_id)

        log_structured_event(
            "tool.intent",
            f"Agent intends to invoke tool '{tool.name}'",
            span=span,
            correlation_id=correlation_id,
            agent_name=str(tool_context.agent_name),
            tool_name=tool.name,
            intended_args=safe_args,
            invocation_id=str(tool_context.invocation_id),
            session_id=self._session_id_from_ctx(tool_context),
        )
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """Record the actual outcome of a tool call paired with its intent ID."""
        key = self._tool_call_key(tool.name, tool_context)
        entry = self._tool_spans.pop(key, None)
        span, started, correlation_id = (
            entry if entry else (None, time.perf_counter(), "untracked")
        )
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)

        outcome_status = classify_tool_outcome(result)
        intent_succeeded = outcome_status == "success"
        is_failure = outcome_status in _FAILED_TOOL_OUTCOMES

        log_structured_event(
            "tool.outcome",
            f"Tool '{tool.name}' finished with status '{outcome_status}'",
            severity=logging.WARNING if is_failure else logging.INFO,
            span=span,
            correlation_id=correlation_id,
            agent_name=str(tool_context.agent_name),
            tool_name=tool.name,
            intended_args=redact_pii(tool_args),
            outcome_status=outcome_status,
            intent_matched_outcome=intent_succeeded,
            latency_ms=latency_ms,
            outcome_preview=redact_pii(
                {
                    k: result[k]
                    for k in ("status", "error_code", "matching_count", "within_budget")
                    if isinstance(result, dict) and k in result
                }
            ),
            invocation_id=str(tool_context.invocation_id),
            session_id=self._session_id_from_ctx(tool_context),
        )
        if span:
            span.set_attribute("tool.outcome_status", outcome_status)
            span.set_attribute("tool.latency_ms", latency_ms)
            span.set_status(Status(StatusCode.ERROR if is_failure else StatusCode.OK))
            span.end()
        return None

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> Optional[dict[str, Any]]:
        """Capture unexpected tool exceptions cleanly and return guided recovery."""
        key = self._tool_call_key(tool.name, tool_context)
        entry = self._tool_spans.pop(key, None)
        span, started, correlation_id = (
            entry if entry else (None, time.perf_counter(), "untracked")
        )
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)

        log_structured_event(
            "tool.error",
            f"Tool '{tool.name}' raised {type(error).__name__}",
            severity=logging.ERROR,
            span=span,
            correlation_id=correlation_id,
            agent_name=str(tool_context.agent_name),
            tool_name=tool.name,
            intended_args=redact_pii(tool_args),
            error_type=type(error).__name__,
            intent_matched_outcome=False,
            latency_ms=latency_ms,
        )
        if span:
            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, type(error).__name__))
            span.end()

        # Fail safe: convert unexpected exceptions into guided JSON recovery errors.
        return {
            "status": "error",
            "error_code": "TOOL_EXECUTION_FAILURE",
            "message": f"Tool '{tool.name}' encountered an internal error.",
            "recovery_guidance": (
                "Verify all required parameters match the tool docstring schema "
                "and retry once, or explain to the traveler that the lookup is "
                "temporarily unavailable."
            ),
        }
