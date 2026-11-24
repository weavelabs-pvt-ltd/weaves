"""Slice 5.6 load-run helpers: percentiles, Prometheus parsing, docker stats."""

import math
from datetime import UTC, datetime

import pytest

from tenancy_harness.phase5 import load_stats as s
from tenancy_harness.phase5.noisy_neighbor import ENVELOPE, verdict


def test_percentile_interpolates_and_handles_edges():
    assert s.percentile([], 50) is None
    assert s.percentile([7], 95) == 7
    assert s.percentile([1, 2, 3, 4], 50) == 2.5
    assert s.percentile([1, 2, 3, 4, 5], 100) == 5
    with pytest.raises(ValueError):
        s.percentile([1], 101)


def test_summarize_reports_milliseconds_and_empty():
    out = s.summarize([0.1, 0.2, 0.3])
    assert out["n"] == 3 and out["p50"] == 0.2 and out["max"] == 0.3
    assert s.summarize([])["p95"] is None


def test_turn_timings_and_throughput():
    created = datetime(2026, 1, 1, tzinfo=UTC)
    started = s.parse_iso("2026-01-01T00:00:04Z")
    finished = s.parse_iso("2026-01-01T00:00:10+00:00")
    assert s.turn_timings(created, started, finished) == {
        "queue_delay": 4.0,
        "completion": 10.0,
    }
    assert s.turn_timings(created, None, None)["queue_delay"] is None
    assert s.throughput_per_minute([1.0] * 30, 150) == 12.0
    with pytest.raises(ValueError):
        s.throughput_per_minute([], 0)


def test_prometheus_parsing_labels_sums_and_quantiles():
    text = "\n".join(
        [
            "# HELP running Records currently running.",
            "# TYPE running gauge",
            'running{queue="turns"} 2',
            'running{queue="jobs"} 1',
            "db_pool_in_use 3",
            'm_bucket{le="1"} 5',
            'm_bucket{le="5"} 9',
            'm_bucket{le="+Inf"} 10',
            "bad line",
        ]
    )
    samples = s.parse_prometheus(text)
    assert s.metric(samples, "running") == 3
    assert s.metric(samples, "running", queue="turns") == 2
    assert s.metric(samples, "db_pool_in_use") == 3
    assert s.metric(samples, "absent") is None
    assert s.histogram_quantile(samples, "m", 0.5) == 1
    assert s.histogram_quantile(samples, "m", 0.9) == 5
    assert s.histogram_quantile(samples, "m", 0.99) is None  # beyond the last bound


def test_docker_stats_row_and_peaks():
    row = {
        "Name": "weaves-tenancy-abc123-worker-1",
        "CPUPerc": "93.5%",
        "MemUsage": "512.5MiB / 1GiB",
    }
    assert s.stats_row(row) == ("worker", 93.5, 512.5)
    assert math.isclose(s.parse_mebibytes("1.5GiB"), 1536)
    assert math.isclose(s.parse_mebibytes("2048kB"), 2.0)
    with pytest.raises(ValueError):
        s.parse_mebibytes("lots")
    peaks = s.Peaks()
    peaks.add("api", 10, 100)
    peaks.add("api", 30, 80)
    assert peaks.report()["api"] == {
        "samples": 2,
        "cpu_peak_percent": 30.0,
        "cpu_mean_percent": 20.0,
        "mem_peak_mib": 100.0,
    }


def _result(p95, maximum, failed=0, statuses=None):
    summary = {"n": 9, "mean": 1, "p50": 1, "p95": p95, "p99": p95, "max": maximum}
    turns = {
        "queue_delay_seconds": summary,
        "completion_seconds": summary,
        "statuses": {"failed": failed},
    }
    quick = {**summary, "p95": 0.2}
    poll = {"latency_seconds": {"/overview": quick}}
    return turns, poll, {"statuses": statuses or {202: 20}}


def test_envelope_verdict_names_each_breached_bound():
    assert verdict(*_result(2.0, 5.0))["within_envelope"]
    slow = verdict(*_result(ENVELOPE["b_queue_delay_p95"] + 1, 5.0))
    assert slow["within_envelope"] is False
    assert slow["checks"]["b_queue_delay_p95"] is False
    assert slow["checks"]["b_queue_delay_max"] is True
    assert not verdict(*_result(1.0, 1.0, failed=1))["within_envelope"]
    broken = verdict(*_result(1.0, 1.0, statuses={201: 5, 500: 1}))
    assert broken["within_envelope"] is False
    assert verdict(*_result(None, None))["within_envelope"] is False
