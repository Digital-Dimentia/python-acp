"""Tests for `scripts/start-ws.sh`, the launcher every `make` target goes through.

Two failure modes brought this file into existence, and both were found live rather than
here, because both are invisible from a terminal:

* **An empty argument reached argparse** (`pyacp-v3u`). The catch-all branch forwarded
  every unrecognised token, `''` included, and argparse answered `unrecognized arguments:`
  with nothing after the colon and exit 2. A client that builds its command line by
  joining a list turns a blank row in a config form into exactly that, so the operator
  sees an agent that died before writing a byte and no statement of why.
* **Pre-launch failures never reached `--log`** (`pyacp-ka3`). The venv check and
  `source activate` ran ahead of the banner, so the one durable record of a launch that
  never happened was an empty file.

Both are asserted against a *fake* venv — a directory with a `bin/python` that prints its
own argv — rather than the repo's. That keeps the tests hermetic, lets a missing or broken
interpreter be arranged deliberately, and makes the forwarded argv directly observable,
which is the only way to prove a pass-through drops one thing and nothing else.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
START_WS = REPO_ROOT / "scripts" / "start-ws.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="no bash on PATH")

#: A stand-in interpreter that reports its argv, one token per line, and exits 0. The
#: script invokes it as `$python_bin -m python_acp.cli <cli args...>`, so the tail of that
#: listing is exactly what the pass-through decided to forward.
FAKE_PYTHON = """#!/usr/bin/env bash
printf '%s\\n' "$@"
"""


def make_venv(root: Path, *, interpreter: str | None = FAKE_PYTHON, activate: str = "") -> Path:
    """Build a fake `VENV_DIR`.

    `interpreter=None` omits `bin/python` entirely, which is the missing-venv case; a
    non-empty `activate` body is how the failing-activate case is arranged.
    """
    venv = root / "venv"
    (venv / "bin").mkdir(parents=True)
    if interpreter is not None:
        python = venv / "bin" / "python"
        python.write_text(interpreter)
        python.chmod(0o755)
    (venv / "bin" / "activate").write_text(activate)
    return venv


def run(venv: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["VENV_DIR"] = str(venv)
    return subprocess.run(
        ["bash", str(START_WS), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def forwarded(result: subprocess.CompletedProcess[str]) -> list[str]:
    """The CLI arguments the fake interpreter was handed, past `-m python_acp.cli`."""
    argv = result.stdout.splitlines()
    assert argv[:2] == ["-m", "python_acp.cli"], argv
    return argv[2:]


# --------------------------------------------------------------------------------------
# pyacp-v3u — the empty argument
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        pytest.param(["", "--transport", "stdio"], ["--transport", "stdio"], id="first"),
        pytest.param(["--transport", "", "stdio"], ["--transport", "stdio"], id="middle"),
        pytest.param(["--transport", "stdio", ""], ["--transport", "stdio"], id="last"),
        pytest.param(["--", "", "--debug"], ["--debug"], id="after-double-dash"),
        pytest.param(["--debug", "--", ""], ["--debug"], id="trailing-after-double-dash"),
        pytest.param([""], [], id="only"),
        pytest.param(["", ""], [], id="several"),
    ],
)
def test_empty_arguments_are_dropped(tmp_path: Path, args: list[str], expected: list[str]) -> None:
    """`''` never reaches the CLI, wherever it appears — `--` does not exempt it."""
    result = run(make_venv(tmp_path), *args)
    assert result.returncode == 0, result.stderr
    assert forwarded(result) == expected


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        pytest.param(["--nonsense"], ["--nonsense"], id="unknown-flag"),
        pytest.param([" "], [" "], id="whitespace"),
        pytest.param(
            ["--host", "0.0.0.0", "--port", "8765"],
            ["--host", "0.0.0.0", "--port", "8765"],
            id="ordinary",
        ),
        pytest.param(["--", "--log"], ["--log"], id="log-after-double-dash"),
    ],
)
def test_non_empty_arguments_pass_through_untouched(
    tmp_path: Path, args: list[str], expected: list[str]
) -> None:
    """The script stays a pass-through: only the empty string is special.

    A whitespace-only argument is in here because it is the near miss — it *looks* like
    the empty one and is not, so it must still be forwarded. So is `--log` after `--`:
    past the separator it is the CLI's argument, not the script's.
    """
    result = run(make_venv(tmp_path), *args)
    assert result.returncode == 0, result.stderr
    assert forwarded(result) == expected


# --------------------------------------------------------------------------------------
# pyacp-ka3 — pre-launch failures reach the log
# --------------------------------------------------------------------------------------


def test_missing_interpreter_names_itself_in_the_log(tmp_path: Path) -> None:
    """The banner and the diagnostic both land in `--log`, not stderr alone.

    This is the whole bead: the log is written before the venv check, so a launch that
    never happened leaves a record instead of an empty file.
    """
    venv = make_venv(tmp_path, interpreter=None)
    log = tmp_path / "logs" / "acp.log"

    result = run(venv, f"--log={log}")

    assert result.returncode == 1
    assert result.stdout == ""
    contents = log.read_text()
    assert "--- python-acp start " in contents
    assert "no interpreter at" in contents
    assert str(venv / "bin" / "python") in contents
    # stderr keeps saying it too, for whoever is watching one.
    assert "no interpreter at" in result.stderr


def test_failing_activate_names_itself_in_the_log(tmp_path: Path) -> None:
    """A broken `activate` is diagnosed rather than aborting mutely under `set -e`."""
    venv = make_venv(tmp_path, activate="return 3\n")
    log = tmp_path / "logs" / "acp.log"

    result = run(venv, f"--log={log}")

    assert result.returncode == 3
    assert result.stdout == ""
    contents = log.read_text()
    assert "--- python-acp start " in contents
    assert "failed to activate" in contents
    assert "failed to activate" in result.stderr


def test_log_directory_is_created_before_the_venv_check(tmp_path: Path) -> None:
    """`--log` into a directory that does not exist yet still records the failure."""
    venv = make_venv(tmp_path, interpreter=None)
    log = tmp_path / "deep" / "nested" / "acp.log"

    result = run(venv, f"--log={log}")

    assert result.returncode == 1
    assert "no interpreter at" in log.read_text()


def test_pre_launch_failure_without_log_still_exits_and_speaks(tmp_path: Path) -> None:
    """No `--log` is not an error path of its own: stderr and the exit code are unchanged."""
    result = run(make_venv(tmp_path, interpreter=None))

    assert result.returncode == 1
    assert result.stdout == ""
    assert "no interpreter at" in result.stderr
