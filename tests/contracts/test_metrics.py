"""Prometheus text, label policy and the cardinality bound."""

from __future__ import annotations

import pytest

from shared import metrics
from shared.metrics import (
    FORBIDDEN_LABELS,
    MAX_SERIES_PER_METRIC,
    Counter,
    Gauge,
    Histogram,
    Registry,
    status_class,
)


@pytest.mark.parametrize("label", sorted(FORBIDDEN_LABELS))
def test_tenant_and_record_identifiers_can_never_be_labels(label: str) -> None:
    with pytest.raises(ValueError, match="high-cardinality"):
        Counter("bad_total", "x", (label,))


def test_declared_metrics_have_no_tenant_labels() -> None:
    for metric in metrics.REGISTRY._metrics.values():
        assert not FORBIDDEN_LABELS.intersection(metric.labels)
    assert "org_id" not in metrics.REGISTRY.render()


def test_counter_gauge_histogram_render_prometheus_text() -> None:
    registry = Registry()
    counter = registry.add(Counter("things_total", "Things.", ("outcome",)))
    gauge = registry.add(Gauge("depth", "Depth.", ("queue",)))
    histogram = registry.add(Histogram("work_seconds", "Work.", buckets=(0.1, 1)))
    counter.inc(outcome="ok")
    counter.inc(2, outcome="ok")
    gauge.set(4, queue="task")
    histogram.observe(0.05)
    histogram.observe(0.5)
    text = registry.render()
    assert "# TYPE things_total counter" in text
    assert 'things_total{outcome="ok"} 3' in text
    assert 'depth{queue="task"} 4' in text
    assert 'work_seconds_bucket{le="0.1"} 1' in text
    assert 'work_seconds_bucket{le="1"} 2' in text
    assert 'work_seconds_bucket{le="+Inf"} 2' in text
    assert "work_seconds_count 2" in text
    assert text.endswith("\n")


def test_wrong_labels_negative_counts_and_duplicates_are_refused() -> None:
    counter = Counter("c_total", "c", ("a",))
    with pytest.raises(ValueError):
        counter.inc(b="x")
    with pytest.raises(ValueError):
        counter.inc(-1, a="x")
    registry = Registry()
    registry.add(counter)
    with pytest.raises(ValueError, match="duplicate"):
        registry.add(counter)


def test_label_values_are_escaped() -> None:
    counter = Counter("e_total", "e", ("route_template",))
    counter.inc(route_template='a"b')
    assert 'route_template="a\\"b"' in "\n".join(counter.render())


def test_series_count_is_bounded() -> None:
    counter = Counter("many_total", "m", ("route_template",))
    for index in range(MAX_SERIES_PER_METRIC + 50):
        counter.inc(route_template=f"/r/{index}")
    rendered = "\n".join(counter.render())
    assert rendered.count("many_total{") <= MAX_SERIES_PER_METRIC + 1
    assert 'route_template="other"' in rendered


def test_callback_gauge_and_failing_callback() -> None:
    gauge = Gauge("cb", "cb")
    gauge.set_callback(lambda: 7)
    assert "cb 7" in gauge.render()
    gauge.set_callback(lambda: 1 / 0)
    assert gauge.render() == []


def test_status_class_and_collectors_survive_failures() -> None:
    assert status_class(404) == "4xx" and status_class(200) == "2xx"
    calls: list[str] = []
    metrics.register_collector(lambda: 1 / 0)
    metrics.register_collector(lambda: calls.append("ran"))
    try:
        metrics.run_collectors()
    finally:
        metrics._collectors.clear()
    assert calls == ["ran"]
