"""Where a session is allowed to look, and what a path has to be to get there.

ACP requires `cwd` and every entry in `additionalDirectories` to be **absolute**. That is
one of two rules this module owns. The other is the one the spec does not write down: a
session declares a set of roots, and *containment* is what makes that declaration mean
anything. `turn_mcp_router.py`'s `fs/read_text_file` and `fs/write_text_file` calls are
the first callers, and `pyacp-3rw.4` settles the rule here rather than there so it is one
rule rather than one per call site.

## Two different operations, deliberately not merged

| | What it does | Touches the filesystem |
|---|---|---|
| **Normalise** (`normalize_root`) | Makes a declared path absolute-and-tidy: collapses `.` and `..` lexically | no |
| **Resolve** (`is_contained`) | Follows symlinks to where a path really points | yes |

A declared root is **normalised and stored**, never resolved. On macOS `/tmp` resolves to
`/private/tmp`, and echoing that back in `session/list` for a client that said `/tmp`
would be answering a question nobody asked.

A *candidate* being checked is **resolved**, and so is each root at the moment of the
check. Comparing unresolved paths would let a symlink inside `cwd` point at `/etc/shadow`
and pass — which is the entire attack this rule exists to stop, and the reason `..`
handling alone is not enough.

## What this does not promise

**It is a check, not a lock.** Resolution happens at check time; a path that passes can
become a symlink out of the tree a microsecond later. Closing that needs the file
descriptor that was actually opened (`openat`/`O_NOFOLLOW`), which belongs with the code
doing the opening. Recorded so nobody reads containment as stronger than it is.

That code turned out not to exist on this side of the wire. Phase 4.2 (`pyacp-8bv.2`) was
where the fix was expected to land, and it landed as calls to the **client's** `fs/*`
methods — this process opens nothing, so `O_NOFOLLOW` has nothing to attach to. The
caller sends the resolved path so the client need not re-walk links already walked here;
the rest belongs to the client's implementation. See `turn_mcp_router.md`.

**Existence is not required.** ACP asks for an absolute path, not an extant one, and a
client may legitimately name a directory it is about to create. `resolve(strict=False)`
handles the missing-path case lexically.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from pathlib import Path

logger = logging.getLogger(__name__)


class PathConstraintError(ValueError):
    """A path that breaks one of this module's two rules.

    A `ValueError`, so `errors.to_request_error` answers `-32602` with the reason in
    `data` — the client sent a path it may not send, which is a parameter problem, and
    nothing has to special-case the type for it to reach the wire correctly.
    """


def normalize_root(path: str, label: str) -> str:
    """Check one declared root and return it tidied.

    Absolute is the ACP requirement. The lexical `..` collapse on top of it is ours, and
    it matters for a reason that is easy to miss: a root written `/home/u/project/..`
    *is* `/home/u`, and storing it verbatim would leave a session whose declared boundary
    reads narrower than the one it actually enforces.
    """
    if not path:
        raise PathConstraintError(f"{label} must not be empty")
    candidate = Path(path)
    if not candidate.is_absolute():
        raise PathConstraintError(
            f"{label} must be an absolute path, got {path!r}"
        )
    return str(Path(_lexically_normal(candidate)))


def normalize_roots(
    cwd: str, additional_directories: Iterable[str] | None = None
) -> tuple[str, tuple[str, ...]]:
    """Validate and tidy a session's declared roots.

    Duplicates are dropped and order is kept: a client that names the same directory
    twice meant one boundary, and the order is what `session/list` echoes back.

    A directory already inside `cwd` is **not** dropped. It is redundant for containment,
    but removing it would change what the client sees in `SessionInfo`, and a client that
    listed it may be relying on it staying listed if `cwd` later narrows.
    """
    root = normalize_root(cwd, "cwd")
    seen: dict[str, None] = {}
    for index, directory in enumerate(additional_directories or ()):
        seen.setdefault(normalize_root(directory, f"additionalDirectories[{index}]"), None)
    return root, tuple(seen)


def is_contained(candidate: str, roots: Sequence[str]) -> bool:
    """Whether `candidate` lies at or under one of `roots`, symlinks followed.

    Both sides are resolved, and that is the point: an unresolved comparison passes a
    symlink inside `cwd` that points anywhere at all. `..` is handled by resolution too,
    so there is no separate traversal check to keep in step with this one.

    An empty `roots` is `False`, not "anything goes" — a session with no declared
    boundary permits nothing, which is the safe reading of a caller that forgot to pass
    the roots.
    """
    resolved = Path(candidate).resolve()
    for root in roots:
        resolved_root = Path(root).resolve()
        if resolved == resolved_root or resolved.is_relative_to(resolved_root):
            return True
    return False


def require_contained(candidate: str, roots: Sequence[str], label: str = "path") -> Path:
    """`is_contained`, as a refusal. Returns the resolved path so a caller can use it.

    Handing back the **resolved** path is deliberate: a caller that re-derived it from
    the original string would be opening something this function never checked.
    """
    if not Path(candidate).is_absolute():
        raise PathConstraintError(f"{label} must be an absolute path, got {candidate!r}")
    if not is_contained(candidate, roots):
        raise PathConstraintError(
            f"{label} {candidate!r} is outside this session's directories: {list(roots)}"
        )
    return Path(candidate).resolve()


def _lexically_normal(path: Path) -> str:
    """Collapse `.` and `..` without touching the filesystem.

    `os.path.normpath` in `PurePath` clothing. Not `resolve()`: this runs on a path the
    client *declared*, and resolving would replace what they said with where it points.
    """
    parts: list[str] = []
    for part in path.parts[1:]:
        if part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return str(Path(path.parts[0], *parts)) if parts else path.parts[0]
