"""Tests for the container-image builder, because it is a gate that fails open.

`scripts/container_image.py` decides whether to build a container and, when it decides
not to, exits 0 — the same silent-success shape that `tests/test_check_docs.py` exists to
guard against. Two failures it was written to prevent are invisible without a test:

* an engine that is on PATH but cannot reach its backend being treated as usable, which
  is what the old `command -v podman || command -v docker` recipe did. On this project's
  development machine that turned a skip into a hard exit-125 build failure, and on any
  machine with a stopped `podman machine` or a dead docker daemon it does the same.
* `save` running after a failed `build`. The old recipe joined them with `;`, so a
  machine holding a `python-acp:local` from an earlier run would export *that* image and
  exit 0 — packaging a stale container that looks exactly like a fresh one.

The second is the dangerous one, and it is the reason `build_and_save` takes an injected
runner: the regression is only observable by asserting on which commands were issued.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "container_image.py"


def load():
    """Import the script by path: `scripts/` is not a package and must not become one."""
    spec = importlib.util.spec_from_file_location("container_image", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


mod = load()


class FakeRunner:
    """Records every command, returning a queued exit code for each."""

    def __init__(self, *codes: int) -> None:
        self.codes = list(codes)
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> int:
        self.calls.append(list(cmd))
        return self.codes.pop(0) if self.codes else 0

    @property
    def verbs(self) -> list[str]:
        """The subcommand of each call — `build`, `save`, ..."""
        return [c[1] for c in self.calls]


# --- the three states -------------------------------------------------------


def test_no_engine_on_path_is_its_own_state(monkeypatch) -> None:
    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)
    status = mod.resolve_engine()
    assert status.state is mod.EngineState.NO_ENGINE
    assert not status.ready
    assert "installed" in status.message()


def test_engine_on_path_but_unreachable_is_not_ready(monkeypatch) -> None:
    """The whole point: found on PATH != usable."""
    monkeypatch.setattr(mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 125, "", "Cannot connect to Podman. dial tcp 127.0.0.1:56416"
        ),
    )
    status = mod.resolve_engine()
    assert status.state is mod.EngineState.UNREACHABLE
    assert not status.ready
    # The message must name the fix, not just the symptom.
    assert "podman machine start" in status.message()
    assert "dial tcp 127.0.0.1:56416" in status.message()


def test_reachable_engine_is_ready(monkeypatch) -> None:
    monkeypatch.setattr(mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "Client: ...\nServer: ...", ""),
    )
    status = mod.resolve_engine()
    assert status.state is mod.EngineState.READY
    assert status.ready


def test_probe_timeout_is_unreachable_not_a_crash(monkeypatch) -> None:
    """A hung VM must not hang the build forever, nor raise past the caller."""

    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="podman version", timeout=1)

    monkeypatch.setattr(mod.subprocess, "run", boom)
    status = mod.probe_engine("/usr/bin/podman", timeout=1)
    assert status.state is mod.EngineState.UNREACHABLE
    assert "timed out" in status.detail


def test_podman_is_preferred_over_docker(monkeypatch) -> None:
    monkeypatch.setattr(mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert mod.find_engine() == "/usr/bin/podman"


# --- the stale-save regression ----------------------------------------------


def test_failed_build_never_saves(tmp_path) -> None:
    """The regression this module was written for.

    With `;` between them a failed build still reached `save`, exporting whatever
    `python-acp:local` happened to be lying around from a previous run.
    """
    runner = FakeRunner(1)  # build fails
    out = tmp_path / "img.tar"
    code = mod.build_and_save(
        "podman", "python-acp:local", Path("Containerfile"), Path("."), out, runner
    )
    assert code == 1
    assert runner.verbs == ["build"], "save must not run after a failed build"
    assert not out.exists()


def test_failed_build_removes_a_stale_tar(tmp_path) -> None:
    """A tar from an earlier successful run must not survive a failed build.

    `release-bundle` bundles `python-acp-container.tar` if the file merely exists, so
    leaving it in place would ship the old image under the new version's name.
    """
    out = tmp_path / "img.tar"
    out.write_bytes(b"stale image from an earlier run")
    code = mod.build_and_save(
        "podman", "python-acp:local", Path("Containerfile"), Path("."), out, FakeRunner(1)
    )
    assert code == 1
    assert not out.exists(), "a stale tar outlived a failed build"


def test_failed_save_leaves_no_partial_tar(tmp_path) -> None:
    out = tmp_path / "img.tar"

    class PartialWriter(FakeRunner):
        def __call__(self, cmd: list[str]) -> int:
            if cmd[1] == "save":
                out.write_bytes(b"truncated")
            return super().__call__(cmd)

    code = mod.build_and_save(
        "podman", "python-acp:local", Path("Containerfile"), Path("."), out, PartialWriter(0, 1)
    )
    assert code == 1
    assert not out.exists(), "a half-written tar looks like a real artifact"


def test_successful_build_saves_in_order(tmp_path) -> None:
    out = tmp_path / "nested" / "img.tar"
    runner = FakeRunner(0, 0)
    code = mod.build_and_save(
        "podman", "python-acp:local", Path("Containerfile"), Path("."), out, runner
    )
    assert code == 0
    assert runner.verbs == ["build", "save"]
    assert runner.calls[0][:4] == ["podman", "build", "-t", "python-acp:local"]
    assert str(out) in runner.calls[1]


# --- skip vs --require ------------------------------------------------------


@pytest.mark.parametrize(
    ("which", "run_result"),
    [
        (lambda _n: None, None),  # no engine
        (lambda n: f"/usr/bin/{n}", subprocess.CompletedProcess([], 125, "", "unreachable")),
    ],
    ids=["no-engine", "unreachable"],
)
def test_both_skip_states_exit_zero_by_default(monkeypatch, capsys, which, run_result) -> None:
    """Packaging must work on a machine with no usable engine."""
    monkeypatch.setattr(mod.shutil, "which", which)
    if run_result is not None:
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: run_result)
    assert mod.main([]) == 0
    assert "skipping container-image export" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("which", "run_result"),
    [
        (lambda _n: None, None),
        (lambda n: f"/usr/bin/{n}", subprocess.CompletedProcess([], 125, "", "unreachable")),
    ],
    ids=["no-engine", "unreachable"],
)
def test_require_turns_every_skip_into_a_failure(monkeypatch, capsys, which, run_result) -> None:
    """A release must not ship without its image and stay quiet about it."""
    monkeypatch.setattr(mod.shutil, "which", which)
    if run_result is not None:
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: run_result)
    assert mod.main(["--require"]) == 1
    assert "--require" in capsys.readouterr().err


# --- the wiring that makes the above true in practice -----------------------


def test_makefile_delegates_and_wires_require() -> None:
    """The recipe must not grow its own copy of the logic again."""
    makefile = (Path(__file__).resolve().parent.parent / "Makefile").read_text()
    recipe = makefile.split("container-image:")[1].split("\n\n")[0]
    assert "scripts/container_image.py" in makefile
    assert "command -v podman" not in makefile, "the two-state PATH check came back"
    assert "$(CONTAINER_FLAGS)" in recipe
    assert "--require" in makefile  # via CONTAINER_FLAGS


def test_release_workflow_requires_the_image() -> None:
    """publish-artifacts.yml is the one caller that must never skip."""
    workflow = (
        Path(__file__).resolve().parent.parent
        / ".github"
        / "workflows"
        / "publish-artifacts.yml"
    ).read_text()
    assert "make container-image REQUIRE_CONTAINER=1" in workflow
