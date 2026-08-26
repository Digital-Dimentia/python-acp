"""Tests for the three launch targets — `run`, `debug`, `stdio`.

They are the ones another program starts: an editor spawning the agent over stdio, a
supervisor bringing up the bridge. That gives them two obligations no other target in the
Makefile has, and both fail in ways a person running `make run` by hand never sees.

**They must work from any working directory.** Something else picks the cwd, and it may
reach the file as `make -f /path/to/python-acp/Makefile run` — where, unlike `-C`, make
does not chdir. Every path in the Makefile is relative, so before this was fixed the
targets died two different ways: `stdio` on `can't open file
'/wherever/scripts/venv_bootstrap.py'`, and `run`/`debug` earlier still, on `No rule to
make target 'pyproject.toml', needed by '.venv/.python-acp-venv.json'` — a *prerequisite*
resolved against the caller's cwd, which no chdir inside the recipe can fix. That is why
none of the three has a `venv` prerequisite any more; they run `$(ENSURE_VENV)` instead.

**`stdio` must write nothing but JSON-RPC to stdout.** Under `--transport stdio` stdout is
the wire (decision B6), and a banner, a `[venv]` line, or make's own echo lands in the
middle of the framing and desynchronizes the client. That is a silent failure in the same
family as `container-image` exiting 0 without building, so it gets the same treatment.

Three layers, catching different regressions:

* `make -n` tests read the *expanded* recipe: a line that forgets its `>&2`, a `cd` that
  goes missing, a `DEBUG=1` that stops reaching the CLI. Nothing is started.
* `make -n` from `tmp_path` exercises the prerequisite bug specifically, since that one is
  make's own path resolution rather than anything inside a recipe.
* `stdio` alone is run for real, end to end, asserting every byte of stdout parses as
  JSON-RPC. `run` and `debug` are not: they would need a listening port, and `bind()` is
  denied in this project's sandbox — the same constraint `tests/test_transport_ws.py`
  works around with `socketpair()`. What is left unproven for those two is only the part
  after `exec`, which is the same start script `stdio` does exercise.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from acp import PROTOCOL_VERSION

REPO_ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = REPO_ROOT / "Makefile"

#: The targets another program launches, and the only ones these rules apply to.
LAUNCH_TARGETS = ("run", "debug", "stdio")

#: One well-formed request, so the agent has something to answer and the test can assert
#: on a real reply rather than only on silence. Stdin then hits EOF, which is how a
#: stdio client hangs up, and `run_stdio` returns.
INITIALIZE = (
    json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": {}},
        }
    )
    + "\n"
)

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="no make on PATH")


def make_env() -> dict[str, str]:
    """The environment for a nested make, with the outer make's flags stripped.

    `make test` exports MAKEFLAGS, and a child make merges it into its own. Inheriting it
    would make these tests answer questions about how the suite was invoked — under
    `make -n test` the end-to-end run would execute nothing at all and still pass.
    """
    env = dict(os.environ)
    for name in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKE_TERMOUT", "MAKE_TERMERR"):
        env.pop(name, None)
    return env


def dry_run(target: str = "stdio", *overrides: str, cwd: Path | None = None) -> list[str]:
    """The commands `make <target>` would run, expanded, without running them.

    A `define`d recipe comes back as one line however many backslash continuations it was
    written with, so a line here is a shell command, not a source line.
    """
    args = ["make", "-n", target, *overrides]
    if cwd is not None:
        args[1:1] = ["-f", str(MAKEFILE)]
    result = subprocess.run(
        args,
        cwd=cwd or REPO_ROOT,
        env=make_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return [line for line in result.stdout.splitlines() if line.strip()]


def target_line(target: str) -> str:
    prefix = f"{target}:"
    return next(ln for ln in MAKEFILE.read_text().splitlines() if ln.startswith(prefix))


# --- launchable from anywhere -----------------------------------------------


@pytest.mark.parametrize("target", LAUNCH_TARGETS)
def test_a_launch_target_declares_no_prerequisites(target: str) -> None:
    """Because make resolves a prerequisite's paths against its own cwd, not this file's.

    `venv` here would drag in `$(VENV_STAMP)`, whose prerequisite is a bare
    `pyproject.toml` — unfindable from anywhere else, and fatal before any recipe runs.
    `$(ENSURE_VENV)` does the same work from inside the recipe, after the chdir.
    """
    assert target_line(target).strip() == f"{target}:"


@pytest.mark.parametrize("target", LAUNCH_TARGETS)
def test_a_launch_target_chdirs_before_every_command(target: str) -> None:
    """Each command, because make gives every recipe line a fresh shell and a chdir dies with it."""
    for command in dry_run(target):
        if command.strip().startswith("printf "):
            continue  # writes nothing to the filesystem and reads no path
        assert command.startswith(f"cd '{REPO_ROOT}'"), (
            f"{target}: {command!r} would resolve its paths against the caller"
        )


@pytest.mark.parametrize("target", LAUNCH_TARGETS)
def test_a_launch_target_plans_from_an_unrelated_directory(target: str, tmp_path: Path) -> None:
    """`make -f <path>/Makefile <target>` from elsewhere — how another program calls it.

    This is `-n` rather than a real launch because the bug it guards is make's own, and it
    strikes before any command runs: with a `venv` prerequisite this raised `No rule to
    make target 'pyproject.toml'` and exited 2 even in dry-run mode.
    """
    commands = dry_run(target, cwd=tmp_path)
    assert commands, "the recipe expanded to nothing"
    assert str(tmp_path) not in "\n".join(commands)


# --- stdout is the wire (stdio only) ----------------------------------------


def test_every_stdio_step_but_the_exec_keeps_its_output_off_stdout() -> None:
    """The banner, and the bootstrap's own logging, go to stderr.

    Only the final `exec` may write to stdout, because at that point stdout *is* the
    protocol. Everything before it is diagnostics and must be redirected — including
    `venv_bootstrap.py`, which logs its `[venv] ...` lines to stdout like any other
    script and does not know what it is being run under.
    """
    *setup, launch = dry_run("stdio")

    assert "&& exec " in launch, f"the last step should be the exec, got {launch!r}"
    for command in setup:
        assert command.rstrip().endswith(">&2"), f"{command!r} would write to the wire"


def test_the_stdio_bootstrap_runs_with_its_stdout_folded_onto_stderr() -> None:
    commands = dry_run("stdio")
    bootstrap = [c for c in commands if "venv_bootstrap.py" in c]
    assert len(bootstrap) == 1, f"expected exactly one bootstrap step, got {bootstrap}"
    assert "1>&2" in bootstrap[0]


# --- what reaches the CLI ---------------------------------------------------


@pytest.mark.parametrize("target", LAUNCH_TARGETS)
def test_a_launch_target_is_phony(target: str) -> None:
    """There is no file by any of these names; without this a stray one would shadow them."""
    phony = next(ln for ln in MAKEFILE.read_text().splitlines() if ln.startswith(".PHONY:"))
    assert f" {target} " in f"{phony} "


def test_the_stdio_transport_is_selected_and_the_socket_flags_are_not_passed() -> None:
    launch = dry_run("stdio")[-1]
    assert "--transport stdio" in launch
    assert "--host" not in launch
    assert "--port" not in launch


def test_no_access_key_is_minted_for_stdio() -> None:
    """A key is admission control for a socket, and there is no socket.

    It would also have to be printed to be useful, and there is nowhere safe to print it.
    `run` and `debug` do mint one — that is `tests/test_transport_ws.py`'s subject.
    """
    recipe = "\n".join(dry_run("stdio"))
    assert "PYTHON_ACP_WS_KEY" not in recipe
    assert "token_urlsafe" not in recipe


def test_debug_is_opt_in_for_stdio() -> None:
    assert "--debug" not in dry_run("stdio")[-1]
    assert "--debug" in dry_run("stdio", "DEBUG=1")[-1]


def test_log_reaches_the_start_script_in_both_spellings() -> None:
    """`LOG=1` means the default path; anything else is the path itself."""
    assert dry_run("stdio", "LOG=1")[-1].endswith("--log")
    assert dry_run("stdio", "LOG=/tmp/acp.log")[-1].endswith("--log=/tmp/acp.log")


# --- end to end -------------------------------------------------------------


def test_running_the_stdio_target_puts_nothing_but_json_rpc_on_stdout() -> None:
    """The whole point of the target, proven by running it.

    Run with `DEBUG=1` deliberately: debug logging is the loudest this process ever gets,
    so it is the case most likely to leak onto the wire if `configure_logging` or the
    start script ever stopped naming stderr explicitly.

    stdout is a pipe here rather than a file because asyncio's write-pipe transport
    rejects a regular file — the same reason `python-acp --transport stdio > out.txt`
    fails. `capture_output=True` gives us pipes on both.
    """
    result = subprocess.run(
        ["make", "stdio", "DEBUG=1"],
        cwd=REPO_ROOT,
        env=make_env(),
        input=INITIALIZE,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, result.stderr

    replies = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert [r["id"] for r in replies] == [1]
    assert replies[0]["result"]["agentInfo"]["name"] == "python-acp"

    # ...and the diagnostics that would have corrupted it all arrived on the other stream.
    assert "[venv]" in result.stderr, "the bootstrap's stdout was not folded onto stderr"
    assert "Starting python-acp (stdio debug)" in result.stderr
    assert "python_acp.transport_stdio" in result.stderr, "debug logging did not run"


def test_the_stdio_target_starts_from_an_unrelated_working_directory(tmp_path: Path) -> None:
    """The same run, launched the way another program would launch it.

    Nothing here is mocked: if any relative path in the recipe resolves against
    `tmp_path`, the agent never starts and there is no reply to read. This is the one
    launch target that can be taken this far without a listening port.
    """
    result = subprocess.run(
        ["make", "-f", str(MAKEFILE), "stdio"],
        cwd=tmp_path,
        env=make_env(),
        input=INITIALIZE,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, result.stderr
    replies = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert [r["id"] for r in replies] == [1]
