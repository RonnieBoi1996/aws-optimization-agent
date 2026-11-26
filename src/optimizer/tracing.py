"""OpenTelemetry spans per graph node (section 10).

Spans are always created (the API is a no-op without an SDK). Set
OPTIMIZER_OTEL_CONSOLE=1 to print them, or wire a real exporter here later.
"""

from __future__ import annotations

import functools
import inspect
import os

from opentelemetry import trace

_configured = False


def setup_tracing() -> None:
    global _configured
    if _configured or os.environ.get("OPTIMIZER_OTEL_CONSOLE") != "1":
        return
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True


tracer = trace.get_tracer("optimizer")


def traced_node(name: str):
    """Decorator: wrap a (sync or async) graph node in a span named node.<name>."""
    def deco(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def a(*args, **kwargs):
                with tracer.start_as_current_span(f"node.{name}"):
                    return await fn(*args, **kwargs)
            return a

        @functools.wraps(fn)
        def s(*args, **kwargs):
            with tracer.start_as_current_span(f"node.{name}"):
                return fn(*args, **kwargs)
        return s
    return deco
