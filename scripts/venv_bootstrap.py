#!/usr/bin/env python3
"""Provision the repo-local virtual environment for python-acp.

The Makefile calls this instead of inlining venv logic in a recipe, because the
rules are conditional in ways `make` expresses badly:

* the venv must be rebuilt when ``$(PYTHON)`` names a different interpreter than
  the one that built it, so ``make venv PYTHON=python3.12 VENV_DIR=.venv312``
  reproduces a CI matrix leg;
* ``pip install`` must run only when it would actually change something, so
  ``make lint``/``make test`` do not reach the network on every invocation;
* ``$(PYTHON)`` is frequently the interpreter *inside* the venv being managed
  (any developer who ran ``source .venv/bin/activate``), which must be resolved
  back to its base interpreter before it is used to build anything.

Run with ``--help`` for the flags. Every mode is stdlib-only and safe to run
with any interpreter >= 3.11.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
STAMP_NAME = ".python-acp-venv.json"
STAMP_SCHEMA = 1
LEGACY_VENV_DIRS = ("venv",)
DIST_NAME = "python-acp"
IMPORT_NAME = "python_acp"

# Emitted by the venv interpreter; keep it stdlib-only and one line of JSON.
_PROBE = (
    "import json,sys;"
    "print(json.dumps({"
    "'version': '%d.%d.%d' % sys.version_info[:3],"
    "'minor': '%d.%d' % sys.version_info[:2],"
    "'executable': sys.executable,"
    "'prefix': sys.prefix,"
    "'base_prefix': sys.base_prefix,"
    "'base_executable': getattr(sys, '_base_executable', None) or '',"
    "}))"
)


class BootstrapError(RuntimeError):
    """A failure that should be reported to the user without a traceback."""


def log(message: str) -> None:
    print(f"[venv] {message}", flush=True)


# --------------------------------------------------------------------------
# interpreter resolution
# --------------------------------------------------------------------------


def probe(executable: str) -> dict[str, str]:
    result = subprocess.run(
        [executable, "-c", _PROBE], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise BootstrapError(
            f"cannot query interpreter {executable!r}:\n{result.stderr.strip()}"
        )
    return json.loads(result.stdout.strip())


def resolve_base_interpreter(python: str) -> dict[str, str]:
    """Return interpreter info for ``python``, stepping out of any venv it lives in.

    Bootstrapping a venv from inside a venv is what makes ``PYTHON ?= python3``
    behave differently for an activated shell than for a bare CI runner: the
    same spelling means two different interpreters. Resolving to the base
    interpreter makes the two cases converge.
    """

    found = shutil.which(python) or python
    if not Path(found).exists():
        raise BootstrapError(
            f"interpreter {python!r} not found on PATH. "
            "Pass an explicit one, e.g. `make venv PYTHON=/usr/bin/python3.12`."
        )

    info = probe(found)
    if info["prefix"] == info["base_prefix"]:
        return info

    base = info["base_executable"]
    if not base or not Path(base).exists():
        raise BootstrapError(
            f"{found} is a virtual environment interpreter and its base interpreter "
            "could not be determined. Re-run with an explicit base interpreter, "
            "e.g. `make venv PYTHON=/usr/bin/python3`."
        )
    log(f"{found} is a venv interpreter; using its base interpreter {base}")
    resolved = probe(base)
    if resolved["prefix"] != resolved["base_prefix"]:
        raise BootstrapError(
            f"resolved base interpreter {base} is itself inside a virtual environment; "
            "pass an explicit PYTHON=<path to a real interpreter>."
        )
    return resolved


# --------------------------------------------------------------------------
# venv lifecycle
# --------------------------------------------------------------------------


def venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":  # pragma: no cover - the Makefile is POSIX-only today
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def is_venv(path: Path) -> bool:
    return (path / "pyvenv.cfg").is_file()


def migrate_legacy_venv(venv_dir: Path) -> None:
    """Rename a pre-pin ``venv/`` to the canonical directory, once.

    ``VENV_DIR`` used to be inferred from whichever directory happened to exist,
    so long-lived checkouts have ``venv/`` while fresh ones (and CI) get
    ``.venv/``. Moving it preserves the installed packages, which matters when
    the network is slow or restricted.
    """

    if venv_dir.exists() or venv_dir.name != ".venv":
        return
    for legacy_name in LEGACY_VENV_DIRS:
        legacy = REPO_ROOT / legacy_name
        if not is_venv(legacy):
            continue
        legacy.rename(venv_dir)
        log(f"migrated legacy {legacy_name}/ to {venv_dir.name}/ (canonical since pyacp-caq)")
        active = os.environ.get("VIRTUAL_ENV")
        if active and Path(active).resolve() == legacy.resolve():
            log(
                f"your shell still has the old {legacy_name}/ activated; "
                f"run `deactivate && source {venv_dir.name}/bin/activate`"
            )
        return


def create_venv(venv_dir: Path, base: dict[str, str]) -> None:
    log(f"creating {venv_dir.name}/ with Python {base['version']} ({base['executable']})")
    subprocess.run([base["executable"], "-m", "venv", str(venv_dir)], check=True)


def ensure_venv(venv_dir: Path, base: dict[str, str], recreate: bool) -> bool:
    """Create/recreate ``venv_dir`` as needed. Returns True when it is brand new."""

    if venv_dir.exists() and not is_venv(venv_dir):
        raise BootstrapError(
            f"{venv_dir} exists but is not a virtual environment (no pyvenv.cfg); "
            "remove it or point VENV_DIR elsewhere."
        )

    if venv_dir.exists():
        reason = None
        if recreate:
            reason = "--recreate requested"
        else:
            current = probe(str(venv_python(venv_dir)))
            if current["minor"] != base["minor"]:
                reason = (
                    f"existing environment is Python {current['version']}, "
                    f"requested {base['version']}"
                )
            else:
                stamp = read_stamp(venv_dir)
                recorded = stamp.get("base_executable") if stamp else None
                if recorded and not same_file(recorded, base["executable"]):
                    reason = (
                        f"existing environment was built from {recorded}, "
                        f"requested {base['executable']}"
                    )
        if reason is None:
            return False
        log(f"rebuilding {venv_dir.name}/: {reason}")
        shutil.rmtree(venv_dir)

    create_venv(venv_dir, base)
    return True


def same_file(left: str, right: str) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:  # pragma: no cover - defensive
        return left == right


# --------------------------------------------------------------------------
# stamp handling
# --------------------------------------------------------------------------


def pyproject_digest() -> str:
    return hashlib.sha256(PYPROJECT.read_bytes()).hexdigest()


def stamp_path(venv_dir: Path) -> Path:
    return venv_dir / STAMP_NAME


def read_stamp(venv_dir: Path) -> dict[str, Any] | None:
    path = stamp_path(venv_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != STAMP_SCHEMA:
        return None
    return data


def expected_stamp(base: dict[str, str]) -> dict[str, Any]:
    return {
        "schema": STAMP_SCHEMA,
        "python": base["version"],
        "base_executable": base["executable"],
        "pyproject_sha256": pyproject_digest(),
    }


def write_stamp(venv_dir: Path, payload: dict[str, Any]) -> None:
    stamp_path(venv_dir).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def stamp_matches(current: dict[str, Any] | None, expected: dict[str, Any]) -> bool:
    if current is None:
        return False
    return all(current.get(key) == value for key, value in expected.items())


# --------------------------------------------------------------------------
# requirement checking (runs inside the target venv)
# --------------------------------------------------------------------------


def project_requirements() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text())
    project = data.get("project", {})
    requirements = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        requirements.extend(extra)
    return requirements


def check_requirements() -> int:
    """``--check-requirements`` mode: report unmet requirements as JSON on stdout.

    Executed *by the venv interpreter*, so it sees that environment's packages.
    """

    from importlib import metadata
    from importlib.util import find_spec

    problems: list[str] = []
    warnings: list[str] = []

    try:
        from packaging.requirements import Requirement
    except ModuleNotFoundError:
        Requirement = None  # noqa: N806 - stand-in for the class

    for raw in project_requirements():
        name = raw
        specifier = None
        if Requirement is not None:
            parsed = Requirement(raw)
            name = parsed.name
            specifier = parsed.specifier
            if parsed.marker is not None and not parsed.marker.evaluate():
                continue
        else:
            for sep in ("==", ">=", "<=", "~=", ">", "<", "!=", "["):
                if sep in name:
                    name = name.split(sep, 1)[0]
            name = name.strip()
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            problems.append(f"{raw}: not installed")
            continue
        if specifier is not None and installed not in specifier:
            problems.append(f"{raw}: installed {installed}")

    try:
        metadata.version(DIST_NAME)
    except metadata.PackageNotFoundError:
        problems.append(f"{DIST_NAME}: not installed (needs `pip install -e '.[dev]'`)")
    else:
        spec = find_spec(IMPORT_NAME)
        origin = Path(spec.origin).resolve().parent.parent if spec and spec.origin else None
        expected = (REPO_ROOT / "src").resolve()
        if origin is not None and origin != expected:
            warnings.append(
                f"{IMPORT_NAME} resolves to {origin}, not this checkout's {expected}"
            )

    print(json.dumps({"problems": problems, "warnings": warnings}))
    return 0


def requirements_report(venv_dir: Path) -> dict[str, list[str]]:
    result = subprocess.run(
        [str(venv_python(venv_dir)), __file__, "--check-requirements"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return {"problems": [f"requirement check failed: {result.stderr.strip()}"], "warnings": []}
    return json.loads(result.stdout.strip())


# --------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------


def pip_install(venv_dir: Path, args: list[str], *, upgrade_pip: bool) -> None:
    python = str(venv_python(venv_dir))
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    trusted = os.environ.get("PIP_TRUSTED_HOST")
    commands = []
    if upgrade_pip:
        commands.append([python, "-m", "pip", "install", "--upgrade", "pip"])
    commands.append([python, "-m", "pip", "install", *args])
    for command in commands:
        result = subprocess.run(command, check=False)
        if result.returncode == 0:
            continue
        if proxy and not trusted:
            log(
                "pip failed with a proxy set in HTTPS_PROXY. If the failure is a TLS "
                "certificate error, the proxy's CA is not in pip's certifi bundle. "
                "Either point pip at that CA (PIP_CERT=/path/to/ca.pem) or, for a "
                "trusted local proxy only, re-run with "
                'PIP_TRUSTED_HOST="pypi.org files.pythonhosted.org".'
            )
        raise BootstrapError(f"command failed: {' '.join(command)}")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def bootstrap(args: argparse.Namespace) -> int:
    venv_dir = Path(args.venv_dir)
    if not venv_dir.is_absolute():
        venv_dir = REPO_ROOT / venv_dir

    migrate_legacy_venv(venv_dir)
    base = resolve_base_interpreter(args.python)
    created = ensure_venv(venv_dir, base, recreate=args.recreate)

    expected = expected_stamp(base)
    current = read_stamp(venv_dir)

    if not created and not args.sync and stamp_matches(current, expected):
        write_stamp(venv_dir, expected)  # refresh mtime so make stops re-running us
        log(f"{venv_dir.name}/ is up to date (Python {base['version']}); skipping pip")
        return 0

    # A venv that exists but has never been stamped (a migrated one, or one a
    # developer built by hand) may already satisfy the project. Adopt it rather
    # than reaching the network. A *stale* stamp means pyproject.toml changed,
    # so only --offline takes this path there.
    may_adopt = not created and not args.sync and (current is None or args.offline)
    if may_adopt:
        report = requirements_report(venv_dir)
        for warning in report["warnings"]:
            log(f"warning: {warning}")
        if not report["problems"]:
            write_stamp(venv_dir, expected)
            log(f"adopted existing {venv_dir.name}/ (all requirements satisfied); skipping pip")
            return 0
        if args.offline:
            joined = "\n  ".join(report["problems"])
            raise BootstrapError(
                f"--offline requested but {venv_dir.name}/ is missing requirements:\n  {joined}"
            )

    if args.offline:
        raise BootstrapError(
            "--offline requested but the environment must be built from the network "
            f"({'new environment' if created else 'no usable environment'})."
        )

    pip_install(venv_dir, ["-e", ".[dev]"], upgrade_pip=created)
    write_stamp(venv_dir, expected)
    log(f"{venv_dir.name}/ ready (Python {base['version']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv-dir", default=".venv", help="target venv directory")
    parser.add_argument("--python", default="python3", help="interpreter to build the venv from")
    parser.add_argument("--sync", action="store_true", help="always run pip install")
    parser.add_argument("--recreate", action="store_true", help="delete and rebuild the venv")
    parser.add_argument("--offline", action="store_true", help="never touch the network")
    parser.add_argument(
        "--check-requirements",
        action="store_true",
        help="internal: report unmet requirements as JSON (run by the venv interpreter)",
    )
    args = parser.parse_args(argv)

    if args.check_requirements:
        return check_requirements()

    try:
        return bootstrap(args)
    except BootstrapError as exc:
        print(f"[venv] error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"[venv] error: command failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
