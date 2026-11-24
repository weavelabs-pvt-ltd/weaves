"""The disaster-recovery drill refuses to run anywhere but a local throwaway stack."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import dr_drill
from rehearsal import drill_safety, stack
from rehearsal.drill_safety import refusals

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
LOCAL = "unix:///var/run/docker.sock"


def check(environ=None, hostname="laptop.local", endpoint=LOCAL, exists=None, **kw):
    return refusals(
        environ or {}, hostname, endpoint, exists or (lambda p: False), **kw
    )


def test_a_clean_local_machine_is_allowed():
    assert check(stack_name="dr-drill-0123abcd", image="weaves-dr-drill:local") == []
    assert check(endpoint="npipe:////./pipe/docker_engine") == []


@pytest.mark.parametrize(
    "variable,value",
    [
        ("DATABASE_URL", "postgresql://weaves:x@localhost/weaves"),
        ("WEAVES_DATABASE_URL", "postgresql://weaves:x@postgres/weaves"),
        ("CLOUDSDK_CORE_PROJECT", "weaves-510819"),
        ("GOOGLE_CLOUD_PROJECT", "weaves-510819"),
        ("PGHOST", "api.weave-studio.in"),
        ("COMPOSE_PROJECT_NAME", "weaves-single"),
        ("DOCKER_CONTEXT", "weaves-personal"),
    ],
)
def test_environment_that_could_aim_the_drill_at_production_is_refused(variable, value):
    problems = check({variable: value})
    assert problems
    assert all(value not in problem for problem in problems)  # never echo values


def test_any_database_url_is_refused_even_if_it_looks_local():
    problems = check({"DATABASE_URL": "postgresql://u:p@127.0.0.1/db"})
    assert any("database URL" in p for p in problems)


@pytest.mark.parametrize("host", ["tcp://10.0.0.5:2375", "ssh://root@8.231.88.4"])
def test_remote_docker_daemons_are_refused(host):
    assert any("remote" in p for p in check({"DOCKER_HOST": host}))


def test_the_active_docker_context_must_be_local():
    assert check(endpoint="tcp://10.0.0.5:2376")
    assert check(endpoint="unknown")
    assert check(endpoint="ssh://deploy@weaves-personal")


@pytest.mark.parametrize(
    "name", ["weaves-personal", "WEAVES-PERSONAL.c.proj", "x-weaves-single-1"]
)
def test_the_production_vm_hostname_is_refused(name):
    assert any("production VM" in p for p in check(hostname=name))


def test_the_production_runtime_file_marks_the_production_host():
    seen = []

    def exists(path):
        seen.append(path)
        return path == "/opt/weaves/singlevm/runtime.env"

    assert any("production host" in p for p in check(exists=exists))
    assert "/opt/weaves/singlevm/runtime.env" in seen


@pytest.mark.parametrize(
    "name", ["weaves-single", "dr-drill-xyz", "dr-drill-0123abcdef", ""]
)
def test_only_drill_stack_names_are_allowed(name):
    assert any("stack name" in p for p in check(stack_name=name))


def test_production_registry_images_are_refused():
    prod = "asia-south1-docker.pkg.dev/weaves-510819/weaves/backend@sha256:" + "a" * 64
    assert any("production registry" in p for p in check(image=prod))
    local_staging = "asia-south1-docker.pkg.dev/weaves-510819/weaves-staging/backend:x"
    assert check(image=local_staging) == []


def test_all_problems_are_reported_together():
    env = {
        "DATABASE_URL": "x",
        "DOCKER_HOST": "tcp://h",
        "GCLOUD_PROJECT": "weaves-510819",
    }
    assert len(check(env, hostname="weaves-personal", endpoint="tcp://h")) >= 5


# ---- the command line -----------------------------------------------------
@pytest.fixture
def no_docker(monkeypatch):
    """Any docker call or drill run during a refusal is a test failure."""
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("docker or the drill ran despite a refusal")

    monkeypatch.setattr(dr_drill, "docker", forbidden)
    monkeypatch.setattr(dr_drill, "run_drill", forbidden)
    monkeypatch.setattr(dr_drill, "_docker_endpoint", lambda: LOCAL)
    return calls


def test_cli_refuses_a_production_environment_without_touching_docker(
    no_docker, monkeypatch, capsys, tmp_path
):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:secret-pw@8.231.88.4/weaves")
    monkeypatch.setenv("CLOUDSDK_CORE_PROJECT", "weaves-510819")
    output = tmp_path / "out.json"
    assert dr_drill.main(["--output", str(output)]) == 2
    err = capsys.readouterr().err
    assert "refused:" in err and "secret-pw" not in err and "8.231.88.4" not in err
    assert not output.exists() and no_docker == []


def test_cli_refuses_a_remote_docker_daemon(no_docker, monkeypatch, capsys):
    for name in ("DATABASE_URL", "WEAVES_DATABASE_URL", "DOCKER_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(dr_drill, "_docker_endpoint", lambda: "ssh://root@prod")
    assert dr_drill.main([]) == 2
    assert "not a local daemon" in capsys.readouterr().err


def test_cli_refuses_the_production_host_file(no_docker, monkeypatch, capsys):
    for name in ("DATABASE_URL", "WEAVES_DATABASE_URL", "DOCKER_HOST"):
        monkeypatch.delenv(name, raising=False)
    real = Path.exists
    monkeypatch.setattr(
        Path,
        "exists",
        lambda self: str(self) == drill_safety.PRODUCTION_ENV_FILE or real(self),
    )
    assert dr_drill.main([]) == 2
    assert "production host" in capsys.readouterr().err


def test_cli_refuses_a_production_registry_image(no_docker, monkeypatch, capsys):
    for name in ("DATABASE_URL", "WEAVES_DATABASE_URL", "DOCKER_HOST"):
        monkeypatch.delenv(name, raising=False)
    image = "asia-south1-docker.pkg.dev/weaves-510819/weaves/backend@sha256:" + "b" * 64
    assert dr_drill.main(["--image", image]) == 2


@pytest.mark.parametrize(
    "flag", ["--project", "--database-url", "--host", "--vm", "--stack-name"]
)
def test_cli_has_no_option_that_names_a_target(flag, capsys):
    with pytest.raises(SystemExit) as stopped:
        dr_drill.main([flag, "weaves-personal"])
    assert stopped.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_the_drill_never_names_production_or_calls_cloud_tools():
    for name in ("rehearsal/drill.py", "dr_drill.py"):
        source = (SCRIPTS / name).read_text()
        assert "gcloud" not in source
        assert "weaves-single" not in source and "weaves-510819" not in source
    assert re.fullmatch(drill_safety.STACK_NAME, "dr-drill-0123abcd")


def test_stack_extras_reach_every_container():
    s = stack.Stack("dr-drill-0123abcd")
    s.extra_env["WEAVES_TOMBSTONE_DIR"] = "/t"
    s.run_args += ["--volume", "v:/t"]
    flags = s.run_flags()
    assert "--env=WEAVES_TOMBSTONE_DIR=/t" in flags
    assert flags[-2:] == ["--volume", "v:/t"]
