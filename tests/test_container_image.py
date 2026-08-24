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
    """Records every command, returning a queued exit code for each.

    A successful command that names the output path also creates it, standing in
    for the tar a real engine would write. Without that, every build would trip
    the "reported success but produced no tar" check.
    """

    def __init__(self, *codes: int, output: Path | None = None) -> None:
        self.codes = list(codes)
        self.calls: list[list[str]] = []
        self.output = output

    def __call__(self, cmd: list[str]) -> int:
        self.calls.append(list(cmd))
        code = self.codes.pop(0) if self.codes else 0
        if code == 0 and self.output is not None and any(str(self.output) in a for a in cmd):
            self.output.parent.mkdir(parents=True, exist_ok=True)
            self.output.write_bytes(b"image")
        return code

    @property
    def verbs(self) -> list[str]:
        """The subcommand of each call — `build`, `save`, `buildx`, ..."""
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
    runner = FakeRunner(0, 0, output=out)
    code = mod.build_and_save(
        "podman", "python-acp:local", Path("Containerfile"), Path("."), out, runner
    )
    assert code == 0
    assert runner.verbs == ["build", "save"]
    assert runner.calls[0][:4] == ["podman", "build", "-t", "python-acp:local"]
    assert str(out) in runner.calls[1]
    assert out.exists()


def test_success_without_an_artifact_is_a_failure(tmp_path) -> None:
    """An engine that exits 0 and writes nothing must not read as a good build.

    buildx can do exactly this when its output is misconfigured, and the result
    would otherwise be a green release with no image attached to it.
    """
    out = tmp_path / "img.tar"
    code = mod.build_and_save(
        "podman", "python-acp:local", Path("Containerfile"), Path("."), out, FakeRunner(0, 0)
    )
    assert code == 1
    assert not out.exists()


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


# --- multi-platform builds --------------------------------------------------
#
# A single-arch image is the dangerous outcome here, because it is not visibly
# wrong: it builds, exports, passes every check, and then fails to start on a
# Raspberry Pi. So these assert on the command *shape*, which is the only part
# observable without an engine.


def test_engine_kind_is_read_from_the_binary_name() -> None:
    assert mod.engine_kind("/usr/bin/docker") == "docker"
    assert mod.engine_kind("/opt/homebrew/bin/podman") == "podman"
    assert mod.engine_kind("/usr/local/bin/docker.exe") == "docker"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, []),
        ("", []),
        ("linux/arm64", ["linux/arm64"]),
        ("linux/amd64,linux/arm64", ["linux/amd64", "linux/arm64"]),
        (" linux/amd64 , linux/arm64 ", ["linux/amd64", "linux/arm64"]),
        ("linux/amd64,,", ["linux/amd64"]),
    ],
)
def test_platform_list_parsing(raw, expected) -> None:
    assert mod.normalize_platforms(raw) == expected


def plan(engine: str, platforms: list[str]) -> list[list[str]]:
    return mod.plan_commands(
        engine,
        "python-acp:local",
        Path("Containerfile"),
        Path("."),
        Path("dist/img.tar"),
        platforms,
    )


def test_docker_multi_platform_uses_buildx_not_build() -> None:
    """Plain `docker build` silently ignores a multi-platform request."""
    cmds = plan("/usr/bin/docker", ["linux/amd64", "linux/arm64"])
    assert len(cmds) == 1
    assert cmds[0][1] == "buildx"
    assert "--platform" in cmds[0]
    assert cmds[0][cmds[0].index("--platform") + 1] == "linux/amd64,linux/arm64"
    assert any(a.startswith("--output=type=oci") for a in cmds[0])


def test_podman_multi_platform_builds_a_manifest_and_pushes_all() -> None:
    """Without --manifest there is no list; without --all the archive holds one arch."""
    build, push = plan("/opt/homebrew/bin/podman", ["linux/amd64", "linux/arm64"])
    assert "--manifest" in build
    assert build[build.index("--platform") + 1] == "linux/amd64,linux/arm64"
    assert push[1:3] == ["manifest", "push"]
    assert "--all" in push
    assert push[-1].startswith("oci-archive:")


def test_single_platform_keeps_the_plain_build_path() -> None:
    """One platform must not pay for buildx or a manifest list."""
    for engine in ("/usr/bin/docker", "/usr/bin/podman"):
        cmds = plan(engine, ["linux/arm64"])
        assert [c[1] for c in cmds] == ["build", "save"]
        assert "--manifest" not in cmds[0]
        assert cmds[0][cmds[0].index("--platform") + 1] == "linux/arm64"


def test_no_platform_is_a_host_build() -> None:
    cmds = plan("/usr/bin/podman", [])
    assert "--platform" not in cmds[0]
    assert [c[1] for c in cmds] == ["build", "save"]


def test_multi_platform_failure_leaves_no_archive(tmp_path) -> None:
    """The stale-tar rule holds on the buildx path too, which has only one command."""
    out = tmp_path / "img.tar"
    out.write_bytes(b"stale")
    code = mod.build_and_save(
        "/usr/bin/docker",
        "python-acp:local",
        Path("Containerfile"),
        Path("."),
        out,
        FakeRunner(1),
        platforms=["linux/amd64", "linux/arm64"],
    )
    assert code == 1
    assert not out.exists()


# --- the ARMv8.2-A misconception --------------------------------------------


def test_raspberry_pi_platform_is_plain_arm64() -> None:
    """ARMv8.2-A is a superset of ARMv8-A, not a separate build target.

    The Pi 5's Cortex-A76 is ARMv8.2-A and runs a linux/arm64 image natively.
    Neither OCI nor Docker Hub defines an arm64/v8.2 platform -- python:3.11-slim
    publishes arm64/v8 and nothing finer -- and we ship pure Python plus a
    prebuilt pydantic-core wheel built for baseline ARMv8-A, so there is no
    native code that a v8.2 build could specialise. This test exists so that
    reasoning survives the next person who reads "Pi 5" and reaches for a
    third platform.
    """
    assert mod.RASPBERRY_PI_PLATFORM == "linux/arm64"
    assert "8.2" not in mod.RASPBERRY_PI_PLATFORM


def test_nothing_ships_an_armv8_2_platform() -> None:
    """No build may name a platform the registry does not publish.

    Matches platform-shaped strings only. Prose that *warns* against v8.2 must
    keep working -- those comments are the point, not a violation.
    """
    import re

    root = Path(__file__).resolve().parent.parent
    # linux/arm64/v8.2, arm64/v8.2, linux/armv8.2 -- anything that would be
    # passed to --platform rather than written in a sentence.
    bogus = re.compile(r"(linux/)?arm(64)?[/v]v?8\.2", re.IGNORECASE)
    for name in ("Makefile", ".github/workflows/publish-artifacts.yml"):
        for lineno, line in enumerate((root / name).read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # a comment explaining why v8.2 is wrong is not a platform
            assert not bogus.search(line), f"{name}:{lineno} names a nonexistent platform"


def test_release_platforms_cover_amd64_and_the_pi() -> None:
    makefile = (Path(__file__).resolve().parent.parent / "Makefile").read_text()
    line = next(ln for ln in makefile.splitlines() if ln.startswith("RELEASE_PLATFORMS"))
    platforms = mod.normalize_platforms(line.split(":=", 1)[1])
    assert platforms == ["linux/amd64", mod.RASPBERRY_PI_PLATFORM]


def test_release_workflow_builds_multi_arch_with_qemu() -> None:
    """arm64 steps run under emulation on an amd64 runner; without QEMU they fail."""
    workflow = (
        Path(__file__).resolve().parent.parent
        / ".github"
        / "workflows"
        / "publish-artifacts.yml"
    ).read_text()
    assert "docker/setup-qemu-action" in workflow
    assert "docker/setup-buildx-action" in workflow
    # The platform list is read from the Makefile, never duplicated here.
    assert "print-release-platforms" in workflow
