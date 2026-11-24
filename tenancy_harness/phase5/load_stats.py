"""Pure helpers for the slice 5.6 load runs: percentiles, timings, text parsing.

Nothing here touches the network, Docker or the database, so it is unit tested
and shared by the in-container scenarios and the host orchestrator.
"""

import math
import re
from collections.abc import Iterable, Sequence
from datetime import datetime

_UNITS = {
    "b": 1 / 1024**2,
    "kb": 1 / 1024,
    "kib": 1 / 1024,
    "mb": 1,
    "mib": 1,
    "gb": 1024,
    "gib": 1024,
}
_SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(\S+)$")
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def percentile(values: Iterable[float], q: float) -> float | None:
    """Linear-interpolated percentile ``q`` (0 to 100); ``None`` when empty."""
    ordered = sorted(values)
    if not ordered:
        return None
    if not 0 <= q <= 100:
        raise ValueError("q must be between 0 and 100")
    rank = (len(ordered) - 1) * q / 100
    low, high = math.floor(rank), math.ceil(rank)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def summarize(values: Sequence[float]) -> dict[str, float | int | None]:
    """Count, mean, p50, p95, p99 and max, rounded to milliseconds."""
    if not values:
        return {
            "n": 0,
            "mean": None,
            "p50": None,
            "p95": None,
            "p99": None,
            "max": None,
        }

    def r(value: float | None) -> float | None:
        return None if value is None else round(value, 3)

    return {
        "n": len(values),
        "mean": r(sum(values) / len(values)),
        "p50": r(percentile(values, 50)),
        "p95": r(percentile(values, 95)),
        "p99": r(percentile(values, 99)),
        "max": r(max(values)),
    }


def parse_iso(text: str) -> datetime:
    """An ISO timestamp as stored in record payloads (``Z`` or an offset)."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def turn_timings(
    created: datetime, started: datetime | None, finished: datetime | None
) -> dict[str, float | None]:
    """Queue delay (created to started) and completion latency (created to finished)."""
    return {
        "queue_delay": None if started is None else (started - created).total_seconds(),
        "completion": None
        if finished is None
        else (finished - created).total_seconds(),
    }


def throughput_per_minute(finished_at: Sequence[float], window_seconds: float) -> float:
    """Completed units per minute over the stated window."""
    if window_seconds <= 0:
        raise ValueError("window must be positive")
    return round(len(finished_at) * 60 / window_seconds, 2)


def parse_labels(text: str | None) -> tuple[tuple[str, str], ...]:
    """Prometheus label block ``{a="b"}`` as a sorted tuple of pairs."""
    if not text:
        return ()
    return tuple(sorted((k, v) for k, v in _LABEL.findall(text)))


def parse_prometheus(text: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    """Map ``(name, sorted label pairs)`` to the sample value; comments skipped."""
    samples: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    for line in text.splitlines():
        match = _SAMPLE.match(line.strip())
        if not match or line.startswith("#"):
            continue
        name, labels, value = match.groups()
        try:
            samples[(name, parse_labels(labels))] = float(value)
        except ValueError:
            continue
    return samples


def metric(
    samples: dict[tuple[str, tuple[tuple[str, str], ...]], float],
    name: str,
    **labels: str,
) -> float | None:
    """The sample for ``name`` whose labels include every given pair, summed."""
    wanted = set(labels.items())
    found = [
        value
        for (sample, pairs), value in samples.items()
        if sample == name and wanted <= set(pairs)
    ]
    return sum(found) if found else None


def histogram_quantile(
    samples: dict[tuple[str, tuple[tuple[str, str], ...]], float],
    name: str,
    q: float,
    **labels: str,
) -> float | None:
    """Upper bucket bound holding quantile ``q`` (0 to 1) of ``<name>_bucket``."""
    wanted = set(labels.items())
    buckets: dict[float, float] = {}
    for (sample, pairs), value in samples.items():
        if sample != name + "_bucket" or not wanted <= set(pairs):
            continue
        bound = dict(pairs).get("le")
        if bound is None:
            continue
        key = math.inf if bound == "+Inf" else float(bound)
        buckets[key] = buckets.get(key, 0) + value
    total = buckets.get(math.inf)
    if not total:
        return None
    for bound in sorted(buckets):
        if buckets[bound] >= q * total:
            return None if math.isinf(bound) else bound
    return None


def parse_mebibytes(text: str) -> float:
    """``12.5MiB`` / ``1.2GiB`` / ``512kB`` as mebibytes."""
    match = re.fullmatch(r"\s*([0-9.]+)\s*([A-Za-z]+)\s*", text)
    if not match or match.group(2).lower() not in _UNITS:
        raise ValueError(f"unrecognised size {text!r}")
    return float(match.group(1)) * _UNITS[match.group(2).lower()]


def stats_row(row: dict[str, str]) -> tuple[str, float, float]:
    """One ``docker stats --format json`` row as (service, cpu percent, MiB used)."""
    name = row["Name"]
    parts = name.rsplit("-", 2)  # <project>-<service>-<replica>
    service = parts[1] if len(parts) == 3 else name
    used = row["MemUsage"].split("/", 1)[0]
    return service, float(row["CPUPerc"].rstrip("%")), parse_mebibytes(used)


class Peaks:
    """Running peak and mean CPU percent and memory MiB per service."""

    def __init__(self) -> None:
        """Start with no samples."""
        self._cpu: dict[str, list[float]] = {}
        self._mem: dict[str, list[float]] = {}

    def add(self, service: str, cpu: float, mem: float) -> None:
        """Record one sample."""
        self._cpu.setdefault(service, []).append(cpu)
        self._mem.setdefault(service, []).append(mem)

    def report(self) -> dict[str, dict[str, float]]:
        """Per service: samples, peak and mean CPU, peak memory."""
        return {
            service: {
                "samples": len(self._cpu[service]),
                "cpu_peak_percent": round(max(self._cpu[service]), 1),
                "cpu_mean_percent": round(
                    sum(self._cpu[service]) / len(self._cpu[service]), 1
                ),
                "mem_peak_mib": round(max(self._mem[service]), 1),
            }
            for service in sorted(self._cpu)
        }


def within(value: float | None, bound: float) -> bool:
    """True when a measured value exists and does not exceed its declared bound."""
    return value is not None and value <= bound


def noisy_row(result: dict, peaks: dict[str, dict[str, float]]) -> dict:
    """One line of the capacity table from a noisy-neighbour result and peaks."""
    config, b, a = result["config"], result["b"], result["a"]
    delay = b["queue_delay_seconds"]
    statuses = {str(k): v for k, v in result["a_http"]["statuses"].items()}
    unexpected = sum(
        n
        for http in (result["a_http"], result["b_http"])
        for status, n in http["statuses"].items()
        if str(status) not in {"200", "201", "429"}
    )
    sampler = result["sampler"]
    return {
        "label": config["label"],
        "b_delay_p50": delay["p50"],
        "b_delay_p95": delay["p95"],
        "b_delay_max": delay["max"],
        "b_completion_p95": b["completion_seconds"]["p95"],
        "b_per_min": b["throughput_per_minute"],
        "a_per_min": a["throughput_per_minute"],
        "a_refused": statuses.get("429", 0),
        "errors": b["statuses"].get("failed", 0)
        + a["statuses"].get("failed", 0)
        + unexpected,
        "cpu_peak_percent": {s: v["cpu_peak_percent"] for s, v in peaks.items()},
        "mem_peak_mib": {s: v["mem_peak_mib"] for s, v in peaks.items()},
        "api_pool_peak": sampler["api_pool_in_use_peak"],
        "worker_pool_peak": sampler["worker_pool_in_use_peak"],
        "db_connections_peak": sampler["db_connections_peak"],
        "within_envelope": result["verdict"]["within_envelope"],
    }
