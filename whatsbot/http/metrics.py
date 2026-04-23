"""Prometheus ``/metrics`` endpoint + registry.

Spec §15 lists the exposed series. We deliberately avoid the
``prometheus_client`` dependency — the exposition format is simple
enough to handroll, and Spec §5's four-way subscription lock makes
every extra dependency a liability.

All series live in :class:`MetricsRegistry` — the sole instance is
stored at ``app.state.metrics_registry`` by ``main.create_app`` and
all application-side increments go through it. Tests build a fresh
registry per test to avoid cross-test pollution.

Naming follows Spec §15:

* ``whatsbot_messages_total{direction,kind}`` counter
* ``whatsbot_claude_turns_total{project,model,mode}`` counter
* ``whatsbot_pattern_match_total{severity}`` counter
* ``whatsbot_redaction_applied_total{pattern}`` counter
* ``whatsbot_response_latency_seconds`` histogram
* ``whatsbot_tokens_used_total{project,model}`` counter
* ``whatsbot_session_active_gauge`` gauge
* ``whatsbot_mode_duration_seconds{mode}`` gauge
* ``whatsbot_hook_decisions_total{tool,decision}`` counter
* ``whatsbot_circuit_state{service,state}`` gauge (0/1 per state
  so Prometheus ``sum by state`` aggregates cleanly)

Binding: the endpoint is registered on the main FastAPI app which
is bound to ``settings.bind_host`` (defaults to ``127.0.0.1``) — i.e.
it never leaves localhost via the Cloudflare tunnel. Spec §15 + §4.
"""

from __future__ import annotations

import threading
import time
from bisect import bisect_left
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Histogram buckets for response latency (seconds). Tuned for the
# Spec §20 P95 budget of <700 ms bot-side.
DEFAULT_LATENCY_BUCKETS: Final[tuple[float, ...]] = (
    0.05,
    0.1,
    0.2,
    0.5,
    1.0,
    2.0,
    5.0,
)


Labels = tuple[tuple[str, str], ...]


def _label_key(labels: dict[str, str] | None) -> Labels:
    if not labels:
        return ()
    return tuple(sorted(labels.items()))


def _render_labels(labels: Labels) -> str:
    if not labels:
        return ""
    parts = [f'{k}="{_escape(v)}"' for k, v in labels]
    return "{" + ",".join(parts) + "}"


def _escape(value: str) -> str:
    """Prometheus label-value escape (per exposition format spec)."""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


@dataclass(slots=True)
class _CounterSeries:
    name: str
    help_text: str
    values: dict[Labels, float]


@dataclass(slots=True)
class _GaugeSeries:
    name: str
    help_text: str
    values: dict[Labels, float]


@dataclass(slots=True)
class _HistogramSeries:
    name: str
    help_text: str
    buckets: tuple[float, ...]
    counts: dict[Labels, list[int]]
    sums: dict[Labels, float]
    totals: dict[Labels, int]


class MetricsRegistry:
    """In-memory thread-safe registry for Prometheus-style metrics.

    API is intentionally tiny — increment, set, observe. Each call
    creates the series on first use so callers don't need to
    declare metrics up front.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, _CounterSeries] = {}
        self._gauges: dict[str, _GaugeSeries] = {}
        self._histograms: dict[str, _HistogramSeries] = {}

    # ---- counter -----------------------------------------------------

    def increment(
        self,
        name: str,
        *,
        value: float = 1.0,
        labels: dict[str, str] | None = None,
        help_text: str = "",
    ) -> None:
        key = _label_key(labels)
        with self._lock:
            series = self._counters.get(name)
            if series is None:
                series = _CounterSeries(
                    name=name, help_text=help_text, values={}
                )
                self._counters[name] = series
            elif help_text and not series.help_text:
                series.help_text = help_text
            series.values[key] = series.values.get(key, 0.0) + value

    def counter_value(
        self, name: str, *, labels: dict[str, str] | None = None
    ) -> float:
        with self._lock:
            series = self._counters.get(name)
            if series is None:
                return 0.0
            return series.values.get(_label_key(labels), 0.0)

    # ---- gauge -------------------------------------------------------

    def set_gauge(
        self,
        name: str,
        value: float,
        *,
        labels: dict[str, str] | None = None,
        help_text: str = "",
    ) -> None:
        key = _label_key(labels)
        with self._lock:
            series = self._gauges.get(name)
            if series is None:
                series = _GaugeSeries(
                    name=name, help_text=help_text, values={}
                )
                self._gauges[name] = series
            elif help_text and not series.help_text:
                series.help_text = help_text
            series.values[key] = value

    def gauge_value(
        self, name: str, *, labels: dict[str, str] | None = None
    ) -> float:
        with self._lock:
            series = self._gauges.get(name)
            if series is None:
                return 0.0
            return series.values.get(_label_key(labels), 0.0)

    # ---- histogram ---------------------------------------------------

    def observe(
        self,
        name: str,
        value: float,
        *,
        labels: dict[str, str] | None = None,
        help_text: str = "",
        buckets: tuple[float, ...] = DEFAULT_LATENCY_BUCKETS,
    ) -> None:
        key = _label_key(labels)
        with self._lock:
            series = self._histograms.get(name)
            if series is None:
                series = _HistogramSeries(
                    name=name,
                    help_text=help_text,
                    buckets=buckets,
                    counts={},
                    sums={},
                    totals={},
                )
                self._histograms[name] = series
            elif help_text and not series.help_text:
                series.help_text = help_text
            bucket_counts = series.counts.setdefault(
                key, [0] * len(series.buckets)
            )
            idx = bisect_left(series.buckets, value)
            for i in range(idx, len(series.buckets)):
                bucket_counts[i] += 1
            series.sums[key] = series.sums.get(key, 0.0) + value
            series.totals[key] = series.totals.get(key, 0) + 1

    def histogram_total(
        self, name: str, *, labels: dict[str, str] | None = None
    ) -> int:
        with self._lock:
            series = self._histograms.get(name)
            if series is None:
                return 0
            return series.totals.get(_label_key(labels), 0)

    # ---- rendering ---------------------------------------------------

    def render(self) -> str:
        """Return the full Prometheus text-format exposition."""
        with self._lock:
            lines: list[str] = []
            self._render_counters(lines)
            self._render_gauges(lines)
            self._render_histograms(lines)
        if not lines:
            return ""
        return "\n".join(lines) + "\n"

    # ---- internals ---------------------------------------------------

    def _render_counters(self, out: list[str]) -> None:
        for series in self._counters.values():
            if series.help_text:
                out.append(f"# HELP {series.name} {series.help_text}")
            out.append(f"# TYPE {series.name} counter")
            for labels, value in sorted(series.values.items()):
                out.append(
                    f"{series.name}{_render_labels(labels)} {_fmt_number(value)}"
                )

    def _render_gauges(self, out: list[str]) -> None:
        for series in self._gauges.values():
            if series.help_text:
                out.append(f"# HELP {series.name} {series.help_text}")
            out.append(f"# TYPE {series.name} gauge")
            for labels, value in sorted(series.values.items()):
                out.append(
                    f"{series.name}{_render_labels(labels)} {_fmt_number(value)}"
                )

    def _render_histograms(self, out: list[str]) -> None:
        for series in self._histograms.values():
            if series.help_text:
                out.append(f"# HELP {series.name} {series.help_text}")
            out.append(f"# TYPE {series.name} histogram")
            for labels in sorted(series.counts.keys()):
                bucket_counts = series.counts[labels]
                for upper, count in zip(series.buckets, bucket_counts, strict=False):
                    bucket_labels = dict(labels) | {"le": _fmt_bucket(upper)}
                    out.append(
                        f"{series.name}_bucket{_render_labels(_label_key(bucket_labels))}"
                        f" {count}"
                    )
                # +Inf bucket = total count
                inf_labels = dict(labels) | {"le": "+Inf"}
                total = series.totals.get(labels, 0)
                out.append(
                    f"{series.name}_bucket{_render_labels(_label_key(inf_labels))}"
                    f" {total}"
                )
                out.append(
                    f"{series.name}_sum{_render_labels(labels)}"
                    f" {_fmt_number(series.sums.get(labels, 0.0))}"
                )
                out.append(
                    f"{series.name}_count{_render_labels(labels)}"
                    f" {total}"
                )


def _fmt_number(value: float) -> str:
    """Prometheus prefers plain integer rendering when the number is
    whole — avoids ``1.0`` vs. ``1`` diffs in scraper parsers."""
    if value == int(value):
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _fmt_bucket(upper: float) -> str:
    return _fmt_number(upper)


# ---- ASGI latency middleware --------------------------------------------


class ResponseLatencyMiddleware(BaseHTTPMiddleware):
    """Observe response-latency into a histogram labelled by path prefix
    + status class.

    The label values are coarse on purpose — we want three distinct
    paths + three status classes, not a cardinality explosion from
    raw paths or individual status codes.
    """

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        registry: MetricsRegistry,
        metric_name: str = "whatsbot_response_latency_seconds",
    ) -> None:
        super().__init__(app)
        self._registry = registry
        self._name = metric_name

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - start
        self._registry.observe(
            self._name,
            elapsed,
            labels={
                "path": _bucket_path(request.url.path),
                "status_class": _bucket_status(response.status_code),
            },
            help_text="Response latency in seconds per path + status class",
        )
        return response


def _bucket_path(path: str) -> str:
    """Map a request path to a low-cardinality bucket. Keeps the
    histogram from fanning out on unique URLs."""
    if path.startswith("/webhook"):
        return "webhook"
    if path.startswith("/hook/"):
        return "hook"
    if path == "/metrics":
        return "metrics"
    if path == "/health":
        return "health"
    return "other"


def _bucket_status(code: int) -> str:
    if 200 <= code < 300:
        return "2xx"
    if 300 <= code < 400:
        return "3xx"
    if 400 <= code < 500:
        return "4xx"
    if 500 <= code < 600:
        return "5xx"
    return "other"


__all__ = [
    "DEFAULT_LATENCY_BUCKETS",
    "MetricsRegistry",
    "ResponseLatencyMiddleware",
]
