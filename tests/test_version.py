"""The version is written down twice, so it is checked.

`pyproject.toml` names the version the wheel is built with; `python_acp.__version__` is
what `initialize` reports as `agentInfo.version`. Both are literals, and nothing in the
build makes one follow the other.

`pyacp-xzo` is what that costs when it is left unchecked: the `0.2.0` release was prepared
with `pyproject.toml` bumped and `__init__.py` left at `0.1.0`, so the wheel would have
shipped an agent that introduced itself to every client as the previous release.

**898 tests passed while that was true.** The three that look like they cover it —
`test_agent.py`, `test_transport_ws.py`, `test_transport_stdio.py` — each assert that the
wire carries `__version__`, comparing the constant against itself. That is worth having,
because it pins the *path* from the constant to `agentInfo`, but it can never catch a
constant that is simply wrong. This module supplies the other half.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import version as distribution_version
from pathlib import Path

import pytest

from python_acp import __version__

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_the_agent_version_matches_the_installed_distribution() -> None:
    """The one that would have caught `pyacp-xzo`.

    Distribution metadata is written at install time from `pyproject.toml`, and the venv
    stamp hashes that file — so editing the version invalidates the stamp and the next
    `make` target reinstalls before this runs. A bump on either side reaches this test.
    """
    assert __version__ == distribution_version("python-acp"), (
        "python_acp.__version__ and the installed distribution disagree. Bump both "
        "pyproject.toml and src/python_acp/__init__.py in the same commit; if you did, "
        "re-run `make sync` so the metadata catches up."
    )


@pytest.mark.skipif(not PYPROJECT.is_file(), reason="installed without the source tree")
def test_the_agent_version_matches_pyproject() -> None:
    """Belt and braces, and the better failure message of the two.

    Reads the file rather than the metadata, so it still fails in a working tree whose
    venv is stale — the exact state in which someone bumps one number, runs the tests,
    and is told everything is fine.
    """
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    assert __version__ == declared


def test_the_version_is_a_release_number_rather_than_a_placeholder() -> None:
    """`0.0.0`, an empty string, or a stray `+dirty` suffix all mean something went wrong
    upstream of here — and each would sail past the two comparisons above if it were
    written into both places."""
    assert __version__
    assert __version__ != "0.0.0"
    assert __version__.replace(".", "").isdigit(), "expected a plain N.N.N release version"
