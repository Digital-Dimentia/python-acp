# `paths.py` — where a session is allowed to look

Two rules, and only one of them is in the ACP spec.

**Absolute.** `cwd` and every entry in `additionalDirectories` must be an absolute path.
Refused with `-32602` at the edge — [agent.py](agent.md), on `session/new`, `/fork`,
`/resume`, and `/load` — and nowhere else, so the rule cannot drift between call sites.

**Contained.** A session declares a set of roots, and *containment* is what makes that
declaration mean anything. The spec does not write this rule down; `pyacp-3rw.4` settles
it here rather than in Phase 4.2 so it is one rule rather than one per call site.

## Normalise and resolve are different operations

| | What it does | Touches the filesystem | Applied to |
|---|---|---|---|
| **Normalise** (`normalize_root`) | Makes a declared path absolute-and-tidy: collapses `.` and `..` lexically | no | a root the client **declared** |
| **Resolve** (`is_contained`) | Follows symlinks to where a path really points | yes | a path being **checked**, and each root at check time |

A declared root is normalised and stored, **never resolved**. On macOS `/tmp` resolves to
`/private/tmp`, and echoing that back in `session/list` for a client that said `/tmp`
would be answering a question nobody asked.

The lexical `..` collapse matters for a reason that is easy to miss: a root written
`/home/u/project/..` **is** `/home/u`, and storing it verbatim would leave a session
whose declared boundary reads narrower than the one it actually enforces.

## Both sides resolve, and that is the whole point

A containment check that compares *unresolved* paths passes a symlink inside `cwd`
pointing at `/etc/shadow`. It looks contained by every lexical measure, and every other
test would still be green. So `is_contained` resolves the candidate **and** each root:

- a link inside the tree pointing out → refused;
- a link inside the tree pointing back in → allowed;
- a declared root that is *itself* a link → still a live boundary, not a dead one.

`..` falls out of resolution too, so there is no separate traversal check to keep in step
with this one.

Two smaller rules that are easy to get wrong and are pinned by tests:

- **A prefix match is not containment.** `/tmp/project-secrets` starts with
  `/tmp/project` and is not inside it.
- **No roots permits nothing.** An empty `roots` is `False`, not "anything goes" — a
  caller that forgot to pass them must not get a pass.

## What this does not promise

**It is a check, not a lock.** Resolution happens at check time; a path that passes can
become a symlink out of the tree a microsecond later. Closing that needs the file
descriptor actually opened (`openat`, `O_NOFOLLOW`), which belongs with the code doing
the opening — Phase 4.2 — not here. Recorded so nobody reads containment as stronger
than it is.

**Existence is not required.** ACP asks for an absolute path, not an extant one, and a
client may legitimately name a directory it is about to create. `resolve(strict=False)`
handles a missing path lexically.

## For Phase 4.2

`fs/read_text_file` and `fs/write_text_file` are the first callers. The call is:

```python
from python_acp.paths import require_contained

resolved = require_contained(path, session.roots, "fs/read_text_file path")
```

`session.roots` is `(cwd, *additional_directories)` — see [sessions.py](sessions.md).
`require_contained` hands back the **resolved** path deliberately: a caller that
re-derived it from the original string would be opening something the check never saw.

## Main symbols

| Symbol | Purpose |
|---|---|
| `normalize_root(path, label)` | One declared root: absolute-or-refuse, then tidied. `label` names which input was wrong |
| `normalize_roots(cwd, additional_directories)` | A whole session's declaration; dedupes while keeping order |
| `is_contained(candidate, roots)` | The predicate, symlinks followed |
| `require_contained(candidate, roots, label)` | The refusal, returning the resolved path |
| `PathConstraintError` | A `ValueError`, so [errors.py](errors.md) maps it to `-32602` with no special case |

A directory already inside `cwd` is **not** dropped from `additionalDirectories`. It is
redundant for containment, but removing it would change what the client sees in
`SessionInfo`.

## Tests

`tests/test_paths.py`. The symlink cases are the ones that matter — see above for why —
and they use real `tmp_path` links rather than a fake filesystem, because resolution
behaviour is exactly what is under test.

## Related

- [sessions.py docs](sessions.md) — `Session.roots`, the declaration this checks against
- [agent.py docs](agent.md) — the edge where the absolute rule is enforced
- [errors.py docs](errors.md) — why `PathConstraintError` is a `ValueError`
