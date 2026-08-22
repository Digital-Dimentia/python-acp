"""Tests for the absolute-path and containment rules.

The symlink cases are the ones that matter. A containment check that compares
*unresolved* paths passes a symlink inside `cwd` pointing at anything at all, and every
other test here would still be green — which is why `test_a_symlink_out_of_the_tree_is_refused`
exists and why both sides are resolved rather than only the candidate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from python_acp.errors import to_request_error
from python_acp.paths import (
    PathConstraintError,
    is_contained,
    normalize_root,
    normalize_roots,
    require_contained,
)


# ---------------------------------------------------------------------------
# Absoluteness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["relative/path", "./here", "..", "~/home", ""])
def test_a_path_that_is_not_absolute_is_refused(path: str) -> None:
    with pytest.raises(PathConstraintError):
        normalize_root(path, "cwd")


def test_the_refusal_reaches_the_client_as_invalid_params() -> None:
    """The point of subclassing ValueError: `errors.py` maps it with no special case."""
    error = to_request_error(PathConstraintError("cwd must be an absolute path"))

    assert error.code == -32602
    assert "absolute" in error.data["reason"]


def test_the_label_names_which_input_was_wrong() -> None:
    with pytest.raises(PathConstraintError, match=r"additionalDirectories\[1\]"):
        normalize_roots("/ok", ["/also-ok", "nope"])


# ---------------------------------------------------------------------------
# Normalisation: lexical, never resolved
# ---------------------------------------------------------------------------


def test_dot_dot_in_a_declared_root_is_collapsed() -> None:
    """`/home/u/project/..` *is* `/home/u`, and storing it verbatim would leave a
    session whose declared boundary reads narrower than the one it enforces."""
    assert normalize_root("/home/u/project/..", "cwd") == "/home/u"


def test_single_dots_are_dropped() -> None:
    assert normalize_root("/a/./b/./c", "cwd") == "/a/b/c"


def test_a_root_that_climbs_past_the_top_stays_at_the_root() -> None:
    assert normalize_root("/a/../../..", "cwd") == "/"


def test_a_declared_root_is_not_resolved(tmp_path: Path) -> None:
    """On macOS `/tmp` resolves to `/private/tmp`; echoing that back in `session/list`
    would answer a question the client did not ask."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    assert normalize_root(str(link), "cwd") == str(link)


def test_duplicate_additional_directories_collapse_and_keep_order() -> None:
    _root, extra = normalize_roots("/w", ["/b", "/a/./x", "/b", "/a/x"])

    assert extra == ("/b", "/a/x")


def test_a_directory_already_inside_cwd_is_kept() -> None:
    """Redundant for containment, but removing it would change what `session/list` echoes."""
    _root, extra = normalize_roots("/w", ["/w/inner"])

    assert extra == ("/w/inner",)


# ---------------------------------------------------------------------------
# Containment
# ---------------------------------------------------------------------------


def test_a_root_contains_itself(tmp_path: Path) -> None:
    assert is_contained(str(tmp_path), [str(tmp_path)])


def test_a_descendant_is_contained(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)

    assert is_contained(str(tmp_path / "a" / "b"), [str(tmp_path)])


def test_a_sibling_is_not(tmp_path: Path) -> None:
    (tmp_path / "inside").mkdir()
    (tmp_path / "outside").mkdir()

    assert not is_contained(str(tmp_path / "outside"), [str(tmp_path / "inside")])


def test_a_prefix_match_is_not_containment(tmp_path: Path) -> None:
    """`/tmp/project-secrets` starts with `/tmp/project` and is not inside it."""
    (tmp_path / "project").mkdir()
    (tmp_path / "project-secrets").mkdir()

    assert not is_contained(str(tmp_path / "project-secrets"), [str(tmp_path / "project")])


def test_any_one_root_is_enough(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b" / "deep").mkdir(parents=True)

    assert is_contained(str(tmp_path / "b" / "deep"), [str(tmp_path / "a"), str(tmp_path / "b")])


def test_no_roots_permits_nothing(tmp_path: Path) -> None:
    """A caller that forgot to pass the roots must not get "anything goes"."""
    assert not is_contained(str(tmp_path), [])


def test_dot_dot_cannot_climb_out(tmp_path: Path) -> None:
    (tmp_path / "inside").mkdir()

    escape = tmp_path / "inside" / ".." / ".."

    assert not is_contained(str(escape), [str(tmp_path / "inside")])


def test_dot_dot_that_stays_inside_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)

    assert is_contained(str(tmp_path / "a" / "b" / ".." / "b"), [str(tmp_path)])


def test_a_symlink_out_of_the_tree_is_refused(tmp_path: Path) -> None:
    """The case an unresolved comparison lets through, and the reason both sides resolve.

    A link *inside* the session's directory that points outside it looks contained by
    every lexical measure.
    """
    inside = tmp_path / "inside"
    inside.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    (inside / "escape").symlink_to(secret)

    assert not is_contained(str(inside / "escape"), [str(inside)])


def test_a_symlink_within_the_tree_is_allowed(tmp_path: Path) -> None:
    inside = tmp_path / "inside"
    (inside / "real").mkdir(parents=True)
    (inside / "link").symlink_to(inside / "real")

    assert is_contained(str(inside / "link"), [str(inside)])


def test_a_root_that_is_itself_a_symlink_still_matches(tmp_path: Path) -> None:
    """Both sides resolve, so a declared root that is a link is not a dead boundary."""
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)

    assert is_contained(str(real / "sub"), [str(link)])


def test_a_path_that_does_not_exist_is_judged_lexically(tmp_path: Path) -> None:
    """ACP asks for an absolute path, not an extant one — a client may be about to
    create it."""
    assert is_contained(str(tmp_path / "not-yet" / "file.txt"), [str(tmp_path)])
    assert not is_contained("/nowhere/at/all", [str(tmp_path)])


# ---------------------------------------------------------------------------
# require_contained
# ---------------------------------------------------------------------------


def test_require_contained_returns_the_resolved_path(tmp_path: Path) -> None:
    """A caller that re-derived it from the original string would open something
    this function never checked."""
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")

    assert require_contained(str(tmp_path / "link"), [str(tmp_path)]) == (
        tmp_path / "real"
    ).resolve()


def test_require_contained_refuses_a_relative_path(tmp_path: Path) -> None:
    with pytest.raises(PathConstraintError, match="absolute"):
        require_contained("relative", [str(tmp_path)])


def test_require_contained_names_the_roots_it_checked(tmp_path: Path) -> None:
    (tmp_path / "inside").mkdir()

    with pytest.raises(PathConstraintError, match="outside this session's directories"):
        require_contained("/etc/passwd", [str(tmp_path / "inside")], "fs/read_text_file path")
