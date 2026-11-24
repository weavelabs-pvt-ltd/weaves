"""In-process counters, gauges and histograms rendered as Prometheus text.

Cardinality is controlled by construction: every metric declares its label names
up front, tenant and per-record identifiers are refused as label names, and a
label set can only take values the caller passes from a small fixed vocabulary
(route templates, status classes, outcomes). Per-tenant numbers belong in SQL
reports, never in a time series.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Mapping

# Label names that would explode series count or identify a tenant.
FORBIDDEN_LABELS = frozenset(
    {
        "org",
        "org_id",
        "organization",
        "organization_id",
        "workspace",
        "workspace_id",
        "principal",
        "principal_id",
        "user",
        "user_id",
        "task_id",
        "job_id",
        "turn_id",
        "run_id",
        "request_id",
        "email",
        "path",
    }
)
# Histogram bounds (seconds): HTTP and model calls share them.
DEFAULT_BUCKETS = (0.005, 0.025, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)
MAX_SERIES_PER_METRIC = 500
OVERFLOW = "other"

_NAME = re.compile(r"[a-z_][a-z0-9_]*")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _render_labels(names: tuple[str, ...], values: tuple[str, ...]) -> str:
    if not names:
        return ""
    pairs = ",".join(f'{n}="{_escape(v)}"' for n, v in zip(names, values, strict=True))
    return "{" + pairs + "}"


class _Metric:
    kind = ""

    def __init__(self, name: str, help_text: str, labels: tuple[str, ...]) -> None:
        if not _NAME.fullmatch(name):
            raise ValueError(f"invalid metric name {name!r}")
        forbidden = FORBIDDEN_LABELS.intersection(labels)
        if forbidden:
            raise ValueError(f"{name}: high-cardinality labels {sorted(forbidden)}")
        self.name, self.help, self.labels = name, help_text, labels
        self._lock = threading.Lock()

    def _key(
        self, label_values: dict[str, str], known: Mapping[tuple[str, ...], object]
    ) -> tuple[str, ...]:
        if set(label_values) != set(self.labels):
            raise ValueError(f"{self.name} takes labels {self.labels}")
        key = tuple(str(label_values[name]) for name in self.labels)
        if key not in known and len(known) >= MAX_SERIES_PER_METRIC:
            return (OVERFLOW,) * len(self.labels)
        return key

    def render(self) -> list[str]:
        """Prometheus text lines."""
        raise NotImplementedError

    def header(self) -> list[str]:
        return [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]


class Counter(_Metric):
    """A monotonically increasing count."""

    kind = "counter"

    def __init__(self, name: str, help_text: str, labels: tuple[str, ...] = ()) -> None:
        """Declare the counter and its fixed label names."""
        super().__init__(name, help_text, labels)
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1, **labels: str) -> None:
        """Add ``amount`` (never negative) to the series for ``labels``."""
        if amount < 0:
            raise ValueError("counters only increase")
        with self._lock:
            key = self._key(labels, self._values)
            self._values[key] = self._values.get(key, 0) + amount

    def render(self) -> list[str]:
        """Prometheus text lines."""
        with self._lock:
            items = sorted(self._values.items())
        return self.header() + [
            f"{self.name}{_render_labels(self.labels, key)} {value:g}"
            for key, value in items
        ]


class Gauge(_Metric):
    """A value that goes up and down; may also be computed at scrape time."""

    kind = "gauge"

    def __init__(self, name: str, help_text: str, labels: tuple[str, ...] = ()) -> None:
        """Declare the gauge and its fixed label names."""
        super().__init__(name, help_text, labels)
        self._values: dict[tuple[str, ...], float] = {}
        self._callback: Callable[[], float] | None = None

    def set(self, value: float, **labels: str) -> None:
        """Set the series for ``labels``."""
        with self._lock:
            self._values[self._key(labels, self._values)] = value

    def set_callback(self, callback: Callable[[], float] | None) -> None:
        """Compute an unlabeled gauge at render time (for example pool use)."""
        if self.labels:
            raise ValueError("callback gauges cannot have labels")
        self._callback = callback

    def render(self) -> list[str]:
        """Prometheus text lines."""
        if self._callback is not None:
            try:
                return [*self.header(), f"{self.name} {self._callback():g}"]
            except Exception:
                return []
        with self._lock:
            items = sorted(self._values.items())
        return self.header() + [
            f"{self.name}{_render_labels(self.labels, key)} {value:g}"
            for key, value in items
        ]


class Histogram(_Metric):
    """Cumulative-bucket distribution of observed values (seconds)."""

    kind = "histogram"

    def __init__(
        self,
        name: str,
        help_text: str,
        labels: tuple[str, ...] = (),
        buckets: tuple[float, ...] = DEFAULT_BUCKETS,
    ) -> None:
        """Declare the histogram, its label names and bucket bounds."""
        super().__init__(name, help_text, labels)
        self._buckets = tuple(sorted(buckets))
        self._series: dict[tuple[str, ...], tuple[list[int], float]] = {}

    def observe(self, value: float, **labels: str) -> None:
        """Record one observation."""
        with self._lock:
            key = self._key(labels, self._series)
            counts, total = self._series.get(key, ([0] * (len(self._buckets) + 1), 0.0))
            for index, bound in enumerate(self._buckets):
                if value <= bound:
                    counts[index] += 1
            counts[-1] += 1
            self._series[key] = (counts, total + value)

    def render(self) -> list[str]:
        """Prometheus text lines."""
        with self._lock:
            items = sorted((k, (list(c), s)) for k, (c, s) in self._series.items())
        lines = self.header()
        for key, (counts, total) in items:
            names = (*self.labels, "le")
            for bound, count in zip(self._buckets, counts, strict=False):
                lines.append(
                    f"{self.name}_bucket{_render_labels(names, (*key, f'{bound:g}'))}"
                    f" {count}"
                )
            lines.append(
                f"{self.name}_bucket{_render_labels(names, (*key, '+Inf'))}"
                f" {counts[-1]}"
            )
            base = _render_labels(self.labels, key)
            lines.append(f"{self.name}_sum{base} {total:g}")
            lines.append(f"{self.name}_count{base} {counts[-1]}")
        return lines


class Registry:
    """The metrics a process exposes, rendered together."""

    def __init__(self) -> None:
        """Start empty."""
        self._metrics: dict[str, _Metric] = {}

    def add[M: _Metric](self, metric: M) -> M:
        """Register ``metric``; names are unique."""
        if metric.name in self._metrics:
            raise ValueError(f"duplicate metric {metric.name}")
        self._metrics[metric.name] = metric
        return metric

    def render(self) -> str:
        """The whole registry in Prometheus text exposition format."""
        lines: list[str] = []
        for name in sorted(self._metrics):
            lines.extend(self._metrics[name].render())
        return "\n".join(lines) + "\n"


REGISTRY = Registry()

HTTP_REQUEST_SECONDS = REGISTRY.add(
    Histogram(
        "http_request_seconds",
        "API request duration by route template and status class.",
        ("route_template", "status_class"),
    )
)
QUEUE_DEPTH = REGISTRY.add(
    Gauge(
        "queue_depth", "Queued records by queue (global, not per tenant).", ("queue",)
    )
)
OLDEST_QUEUED_AGE = REGISTRY.add(
    Gauge(
        "oldest_queued_age_seconds",
        "Age of the oldest queued record by queue.",
        ("queue",),
    )
)
RUNNING = REGISTRY.add(
    Gauge("running", "Records currently running by queue.", ("queue",))
)
ADMISSION_REFUSED = REGISTRY.add(
    Counter(
        "admission_refused_total",
        "Work refused at admission by limited resource.",
        ("resource",),
    )
)
MODEL_CALL_SECONDS = REGISTRY.add(
    Histogram("model_call_seconds", "Model provider call duration.")
)
MODEL_CALLS = REGISTRY.add(
    Counter("model_call_total", "Model provider calls by outcome.", ("outcome",))
)
USAGE_TOKENS = REGISTRY.add(
    Counter("usage_tokens_total", "Model tokens used by direction.", ("direction",))
)
DB_POOL_IN_USE = REGISTRY.add(
    Gauge("db_pool_in_use", "Database connections currently checked out.")
)


def status_class(status: int) -> str:
    """``2xx``, ``4xx`` and so on, so statuses never multiply series."""
    return f"{status // 100}xx"


_collectors: list[Callable[[], None]] = []


def register_collector(collector: Callable[[], None]) -> None:
    """Add a refresher the worker's periodic tick runs (for example SQL gauges)."""
    _collectors.append(collector)


def run_collectors() -> None:
    """Run every collector; a failing one is logged and never stops the rest."""
    for collector in tuple(_collectors):
        try:
            collector()
        except Exception:
            logging.getLogger("product.metrics").exception("metrics collector failed")
