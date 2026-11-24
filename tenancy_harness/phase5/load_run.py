"""Host orchestrator for the slice 5.6 load runs (opt-in, long, never in CI).

SIMULATED LOCALLY, NOT MEASURED ON GCP: builds the backend image, starts the
harness stack (PostgreSQL, API, native worker, fake provider) shaped like the
production ``e2-medium`` by ``load.compose.yml`` (two pinned CPUs and memory caps
for the three services that share the VM), then runs the scenarios in the
``seed`` container while sampling ``docker stats`` on the host. It refuses to
run in CI, never reads production configuration and never starts a paid model
call: only the fake provider answers.

    python3 -m tenancy_harness.phase5.load_run --backend . --output .local/load

Results land in ``<output>/<project>/results.json``; the private run directory
(secrets, TLS) is removed with the stack. Wrap heavy runs in the shared lock.
"""

import argparse
import json
import os
import secrets
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from tenancy_harness.phase5.load_stats import (
    Peaks,
    noisy_row,
    stats_row,
    window_peaks,
)
from tenancy_harness.run_support import Compose, execute, revision, setup_private

# (slots, limits mode, per-organization running cap, B seconds between tasks).
# Production is (2, observe, 2, 20). B at 20 s is a normal tenant (3 tasks per
# minute); 6 s is the stress profile that shows where fair claiming runs out.
MATRIX = (
    (1, "enforce", 2, 20),
    (1, "observe", 2, 20),
    (2, "enforce", 2, 20),
    (2, "observe", 2, 20),
    (4, "enforce", 2, 20),
    (4, "observe", 2, 20),
    (2, "enforce", 1, 20),
    (2, "enforce", 2, 6),
    (4, "enforce", 2, 6),
    (2, "enforce", 1, 6),
)
# Seconds after the scenario starts, then (action, services) pairs.
KILL_PLAN = (
    (45, "kill", ("worker",)),
    (75, "kill", ("api",)),
    (105, "stop", ("worker",)),
    (135, "kill", ("api", "worker")),
    (165, "kill", ("worker",)),
    (195, "kill", ("worker",)),
)
SERVICES = ("postgres", "api", "worker", "provider")


class StatsSampler(threading.Thread):
    """``docker stats`` for the project's containers, grouped into phases."""

    def __init__(self, project):
        """Sample only containers whose name starts with this project."""
        super().__init__(daemon=True)
        self.project, self.stop_event = project, threading.Event()
        self.peaks = Peaks()
        self.samples = []  # (taken, service, cpu percent, MiB) for window reports

    def reset(self):
        """Start a fresh phase and return the previous peaks report."""
        report, self.peaks = self.peaks.report(), Peaks()
        return report

    def run(self):
        """Sample every few seconds until stopped."""
        while not self.stop_event.is_set():
            try:
                out = execute(
                    ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                ).stdout
            except (subprocess.SubprocessError, OSError):
                continue
            for line in out.splitlines():
                row = json.loads(line)
                if row["Name"].startswith(self.project):
                    service, cpu, mem = stats_row(row)
                    if service in SERVICES:
                        self.peaks.add(service, cpu, mem)
                        self.samples.append((datetime.now(UTC), service, cpu, mem))
            self.stop_event.wait(1.0)

    def stop(self):
        """Stop sampling."""
        self.stop_event.set()
        self.join(timeout=30)


class Stack:
    """The disposable stack and the per-phase environment it is run with."""

    def __init__(self, compose, private):
        """Remember how to reach compose and where results appear."""
        self.compose, self.private, self.env = compose, private, {}

    def run(self, *args, **kwargs):
        """Compose command with the current experiment variables."""
        return self.compose.run(*args, env=self.env, **kwargs)

    def configure(self, slots, mode, cap, pool=8):
        """Recreate API and worker with new settings (data is kept)."""
        self.env = {
            "LOAD_WORKER_SLOTS": str(slots),
            "LOAD_LIMITS_MODE": mode,
            "LOAD_MAX_RUNNING": str(cap),
            "LOAD_WORKER_POOL": str(pool),
        }
        self.run("up", "-d", "--force-recreate", "--no-deps", "api", "worker")

    def scenario(self, module, *args):
        """Run one scenario module in the seed container; return its exit code."""
        return self.run(
            "run",
            "--rm",
            "--no-deps",
            "seed",
            "python",
            "-m",
            "tenancy_harness.phase5." + module,
            *args,
            check=False,
        ).returncode

    def result(self, label):
        """The JSON a scenario wrote to the private directory."""
        path = self.private / f"load-{label}.json"
        if not path.is_file():
            return {"error": "scenario wrote no result; see service.log"}
        return json.loads(path.read_text())


def noisy_phase(stack, sampler, cell, args):
    """One cell of the matrix; returns the table row plus the full result."""
    slots, mode, cap, interval = cell
    label = f"s{slots}-{mode}-c{cap}-b{interval:g}"
    stack.configure(slots, mode, cap)
    sampler.reset()
    code = stack.scenario(
        "noisy_neighbor",
        "--label",
        label,
        "--duration",
        str(args.duration),
        "--warmup",
        str(args.warmup),
        "--b-interval",
        str(interval),
    )
    sampler.reset()
    result = stack.result(label)
    if "error" in result:
        return {"slots": slots, "mode": mode, "exit_code": code, "result": result}
    peaks = window_peaks(
        sampler.samples,
        datetime.fromisoformat(result["window_start"]),
        datetime.fromisoformat(result["window_end"]),
    )
    return {
        "slots": slots,
        "mode": mode,
        "max_running_per_org": cap,
        "b_interval": interval,
        "exit_code": code,
        "row": noisy_row(result, peaks),
        "result": result,
    }


def restart_plan(stack, started):
    """Kill and restart the real containers on the schedule, relative to start."""
    for offset, action, services in KILL_PLAN:
        time.sleep(max(0.0, started + offset - time.monotonic()))
        flags = {"kill": ("-s", "SIGKILL"), "stop": ("-t", "30")}[action]
        stack.run(action, *flags, *services)
        time.sleep(4)
        stack.run("up", "-d", "--no-deps", *services)


def restart_phase(stack, sampler, args):
    """Stream work while the API and worker are killed, then audit recovery."""
    stack.configure(2, "enforce", 2)
    sampler.reset()
    killer = threading.Thread(
        target=restart_plan, args=(stack, time.monotonic() + 30), daemon=True
    )
    killer.start()
    code = stack.scenario(
        "reservation_recovery", "--label", "restart", "--seconds", str(args.stream)
    )
    killer.join(timeout=60)
    return {
        "exit_code": code,
        "kill_plan": KILL_PLAN,
        "peaks": sampler.reset(),
        "result": stack.result("restart"),
    }


def scale_phase(stack, sampler, args):
    """Tenant-count growth and a very large history, at production settings."""
    stack.configure(2, "enforce", 2)
    sampler.reset()
    code = stack.scenario(
        "capacity_scale",
        "--label",
        "scale",
        "--stages",
        args.stages,
        "--history",
        str(args.history),
    )
    return {
        "exit_code": code,
        "peaks": sampler.reset(),
        "result": stack.result("scale"),
    }


def build(backend, values):
    """Build the backend image only (the frontend is not part of load runs)."""
    execute(["docker", "build", "-t", values["TEST_BACKEND_IMAGE"], str(backend)])


def parse(argv=None):
    """Command line; every default reproduces the numbers in the capacity doc."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--backend", type=Path, required=True)
    p.add_argument("--output", type=Path, default=Path(".local/load"))
    p.add_argument(
        "--phases",
        default="noisy,scale,restart",
        help="comma separated subset of noisy, scale, restart",
    )
    p.add_argument("--duration", type=int, default=300)
    p.add_argument("--warmup", type=int, default=30)
    p.add_argument("--stages", default="10,50,100,200")
    p.add_argument("--history", type=int, default=6000)
    p.add_argument("--stream", type=int, default=240)
    p.add_argument(
        "--cells",
        default="",
        help="cells slots:mode:cap:b_interval, e.g. 2:enforce:2:20 (default all)",
    )
    return p.parse_args(argv)


def cells(spec):
    """Matrix cells selected by ``--cells`` (all of ``MATRIX`` when empty)."""
    if not spec:
        return MATRIX
    return tuple(
        (int(s), mode, int(c), float(b))
        for s, mode, c, b in (item.split(":") for item in spec.split(","))
    )


def main(argv=None):
    """Build, run the chosen phases, write results, always remove the stack."""
    args = parse(argv)
    if os.environ.get("CI"):
        raise RuntimeError("Load runs are local and opt-in; refused in CI")
    backend, harness = args.backend.resolve(), Path(__file__).resolve().parents[1]
    project = "weaves-tenancy-" + secrets.token_hex(6)
    root = args.output.resolve() / project
    root.mkdir(parents=True, mode=0o700)
    private, envfile, _port, values = setup_private(root, harness, project, backend)
    compose = Compose(project, harness, envfile)
    compose.base.extend(["-f", str(harness / "phase5/load.compose.yml")])
    stack, sampler = Stack(compose, private), StatsSampler(project)
    report = {"project": project, "source": revision(backend), "phases": {}}
    try:
        build(backend, values)
        stack.env = {"LOAD_WORKER_SLOTS": "2", "LOAD_LIMITS_MODE": "enforce"}
        stack.run("up", "-d", "postgres", "provider", "api", "worker")
        sampler.start()
        for phase in args.phases.split(","):
            if phase == "noisy":
                report["phases"]["noisy"] = [
                    noisy_phase(stack, sampler, cell, args)
                    for cell in cells(args.cells)
                ]
            elif phase == "scale":
                report["phases"]["scale"] = scale_phase(stack, sampler, args)
            elif phase == "restart":
                report["phases"]["restart"] = restart_phase(stack, sampler, args)
            (root / "results.json").write_text(
                json.dumps(report, indent=2, default=str)
            )
    finally:
        sampler.stop()
        (root / "results.json").write_text(json.dumps(report, indent=2, default=str))
        with (private / "service.log").open("w") as log:
            stack.run("logs", "--no-color", "api", "worker", stdout=log, check=False)
        stack.run("down", "--volumes", "--remove-orphans", check=False)
        execute(["docker", "rmi", values["TEST_BACKEND_IMAGE"]], check=False)
    print(json.dumps([c["row"] for c in report["phases"].get("noisy", [])], indent=2))


if __name__ == "__main__":
    main()
