"""Tests for `scripts/start-zoo.sh`, the schema-zoo launcher.

The script is four lines of behaviour wrapped around `start-ws.sh`, and every one of them
fails *silently* — which is the only reason this file exists.

* **The export is the whole point.** `MOCK_MCP_SCHEMA_ZOO` is read by
  `tests/fixtures/mock_mcp_server.py`, never by this agent, and reaches it only by being
  inherited from the process that spawns it. If the export were dropped the script would
  still start, still hand out a session, and still answer `/zoo/echo` — it would just
  serve one tool where thirteen were advertised, and nothing on screen would say so.
* **A banner on stdout corrupts the wire.** Under `--transport stdio` fd 1 carries
  JSON-RPC. A launcher that greets the operator there produces a client that fails to
  parse the first message, with the cause four lines above the error.

Both are asserted against a *fake* venv — a `bin/python` that reports its argv and the one
variable that matters — following `tests/test_start_ws.py`, which owns the argument-
forwarding and `--log` rules this script inherits rather than reimplements.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
START_ZOO = REPO_ROOT / "scripts" / "start-zoo.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="no bash on PATH")

#: Reports the environment the child was actually given, then its argv. The script's
#: contract is exactly these two things, so the stand-in interpreter observes both.
FAKE_PYTHON = """#!/usr/bin/env bash
printf 'ZOO=%s\\n' "${MOCK_MCP_SCHEMA_ZOO-<unset>}"
printf 'ANNOTATED=%s\\n' "${MOCK_MCP_ANNOTATED_TOOLS-<unset>}"
printf '%s\\n' "$@"
"""


def make_venv(root: Path) -> Path:
    venv = root / "venv"
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin" / "python"
    python.write_text(FAKE_PYTHON)
    python.chmod(0o755)
    (venv / "bin" / "activate").write_text("")
    return venv


def run(venv: Path, *args: str, **overrides: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["VENV_DIR"] = str(venv)
    # A developer with the zoo already exported must not change what the test observes.
    env.pop("MOCK_MCP_SCHEMA_ZOO", None)
    env.pop("MOCK_MCP_ANNOTATED_TOOLS", None)
    env.update(overrides)
    return subprocess.run(
        ["bash", str(START_ZOO), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def child(result: subprocess.CompletedProcess[str]) -> tuple[dict[str, str], list[str]]:
    """The two lines of environment the fake interpreter reported, and its CLI argv."""
    lines = result.stdout.splitlines()
    assert lines[0].startswith("ZOO="), lines
    assert lines[1].startswith("ANNOTATED="), lines
    seen = {"ZOO": lines[0][4:], "ANNOTATED": lines[1][10:]}
    assert lines[2:4] == ["-m", "python_acp.cli"], lines
    return seen, lines[4:]


def test_zoo_variable_is_exported(tmp_path: Path) -> None:
    """The one thing this script is for.

    Asserted on the *child's* environment rather than the script's, because that is where
    it has to arrive: an MCP server named in `session/new` is a grandchild of this
    process and inherits what the CLI was given, not what the shell briefly held.
    """
    seen, _ = child(run(make_venv(tmp_path)))
    assert seen["ZOO"] == "1"


def test_zoo_variable_is_forced_not_defaulted(tmp_path: Path) -> None:
    """An inherited `0` is overridden.

    A script named `start-zoo` that honoured it would serve one `echo` tool while its own
    banner advertised thirteen — the confusing half of both worlds, and a state the
    operator would debug in the fixture rather than here.
    """
    seen, _ = child(run(make_venv(tmp_path), MOCK_MCP_SCHEMA_ZOO="0"))
    assert seen["ZOO"] == "1"


def test_the_sibling_opt_in_is_inherited_untouched(tmp_path: Path) -> None:
    """`MOCK_MCP_ANNOTATED_TOOLS` is neither set nor cleared — the help says so."""
    venv = make_venv(tmp_path)
    unset, _ = child(run(venv))
    assert unset["ANNOTATED"] == "<unset>"
    passed, _ = child(run(venv, MOCK_MCP_ANNOTATED_TOOLS="1"))
    assert passed["ANNOTATED"] == "1"


def test_stdio_transport_is_prepended(tmp_path: Path) -> None:
    _, argv = child(run(make_venv(tmp_path)))
    assert argv == ["--transport", "stdio"]


def test_arguments_are_forwarded_after_the_transport(tmp_path: Path) -> None:
    """Extra flags land *after* `--transport stdio`, so a later one can override it."""
    _, argv = child(run(make_venv(tmp_path), "--debug", "--transport", "ws"))
    assert argv == ["--transport", "stdio", "--debug", "--transport", "ws"]


def test_the_banner_never_touches_stdout(tmp_path: Path) -> None:
    """fd 1 is the protocol wire under stdio; the banner belongs on fd 2.

    Nothing but the fake interpreter's own output may appear on stdout, and the banner's
    most distinctive lines must be on stderr rather than merely absent.
    """
    result = run(make_venv(tmp_path))
    _, argv = child(result)
    assert result.stdout.splitlines()[4:] == argv  # nothing after the forwarded argv
    assert "MOCK_MCP_SCHEMA_ZOO=1" not in result.stdout
    assert "session/new" not in result.stdout
    assert "MOCK_MCP_SCHEMA_ZOO=1" in result.stderr
    assert "session/new" in result.stderr


def test_help_exits_without_launching(tmp_path: Path) -> None:
    """`--help` is answered here, not forwarded to a CLI that would print its own."""
    result = run(make_venv(tmp_path), "--help")
    assert result.returncode == 0, result.stderr
    assert "start-zoo.sh" in result.stdout
    assert "python_acp.cli" not in result.stdout


def test_a_missing_venv_is_reported_by_the_launcher(tmp_path: Path) -> None:
    """The venv rules are `start-ws.sh`'s, and this script inherits them rather than
    growing a second copy — so a missing interpreter must still fail loudly here."""
    empty = tmp_path / "venv"
    (empty / "bin").mkdir(parents=True)
    result = run(empty)
    assert result.returncode == 1
    assert "no interpreter at" in result.stderr
