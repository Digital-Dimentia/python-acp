#!/usr/bin/env bash
#
# Start the python-acp bridge on its WebSocket transport, optionally teeing the
# diagnostics to a file.
#
# Only *stderr* is captured, and stdout is left strictly alone. That is not a detail to
# simplify away: `configure_logging` in cli.py names stderr explicitly because under
# `--transport stdio` stdout is the protocol wire, so the familiar `2>&1 | tee` idiom
# would splice log lines into the JSON-RPC stream and corrupt it. Routing stderr alone
# keeps this script safe to reuse for either transport.

set -euo pipefail

readonly REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly DEFAULT_LOG="${REPO_ROOT}/logs/python-acp-ws.log"

usage() {
    cat <<'USAGE'
Usage: scripts/start-ws.sh [--log[=PATH]] [-- ] [cli args...]

Starts `python -m python_acp.cli` on the WebSocket transport using the repo venv.

Options:
  --log[=PATH]   Also append diagnostics to PATH. The value is optional: bare
                 `--log` writes to logs/python-acp-ws.log. Output still appears
                 on the terminal; only stderr is captured, so stdout stays a
                 clean protocol wire.
  -h, --help     This message.

Every other argument is passed through to the CLI untouched, so the usual flags
work: --host, --port, --debug, --transport.

Environment:
  VENV_DIR       Virtual environment to activate (default: .venv), matching the
                 Makefile knob of the same name.
  PYTHON_ACP_WS_KEY
                 Read by the server itself; clients then connect as
                 ws://host:port/?key=<secret>. A non-loopback --host with
                 neither this nor PYTHON_ACP_WS_ALLOW_UNAUTHENTICATED=1 is
                 refused before the port is bound.

Examples:
  scripts/start-ws.sh --log
  scripts/start-ws.sh --log=/tmp/acp.log --port 8766 --debug
USAGE
}

log_path=""
cli_args=()

while (($# > 0)); do
    case "$1" in
        --log)
            # The value is optional, so a following token is only consumed when it
            # cannot be a flag in its own right.
            if (($# > 1)) && [[ $2 != -* ]]; then
                log_path="$2"
                shift 2
            else
                log_path="$DEFAULT_LOG"
                shift
            fi
            ;;
        --log=*)
            log_path="${1#--log=}"
            if [[ -z $log_path ]]; then
                printf '%s: --log= needs a path (use bare --log for the default)\n' \
                    "${BASH_SOURCE[0]}" >&2
                exit 2
            fi
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        --)
            shift
            cli_args+=("$@")
            break
            ;;
        *)
            cli_args+=("$1")
            shift
            ;;
    esac
done

readonly venv_dir="${VENV_DIR:-${REPO_ROOT}/.venv}"
readonly python_bin="${venv_dir}/bin/python"

if [[ ! -x $python_bin ]]; then
    printf '%s: no interpreter at %s -- run `make venv` first.\n' \
        "${BASH_SOURCE[0]}" "$python_bin" >&2
    exit 1
fi

# Activate, rather than just calling the venv's interpreter by path. The two are the
# same for *this* process, but not for its children: activation exports VIRTUAL_ENV and
# puts $venv_dir/bin at the front of PATH, so an MCP server that a client names in
# `session/new` as a bare `python` resolves to this venv instead of whatever interpreter
# happened to be on the caller's PATH.
# shellcheck disable=SC1091  # resolved at runtime, not a checked-in file
source "${venv_dir}/bin/activate"

# Keep log lines timely when stderr is a pipe rather than a terminal.
export PYTHONUNBUFFERED=1

cd -- "$REPO_ROOT"

# Both invocations below expand argv as `${cli_args[@]+"${cli_args[@]}"}` rather than a
# plain `"${cli_args[@]}"`: macOS ships bash 3.2, where an empty array counts as unbound
# under `set -u`, so a no-argument run would abort before starting anything.

if [[ -n $log_path ]]; then
    log_dir="$(dirname -- "$log_path")"
    mkdir -p -- "$log_dir"
    # A banner per run, because the file is appended to: without it two runs read as
    # one session with an inexplicable rebind in the middle.
    printf -- '--- python-acp start %s ---\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" >>"$log_path"
    printf 'Logging to %s\n' "$log_path" >&2

    # Park the real stdout on fd3, send stderr down the pipe to `tee`, and hand the
    # child fd3 back as its stdout. The obvious `2> >(tee ...)` is avoided because
    # process substitution needs /dev/fd, which a sandboxed or restricted shell may
    # refuse; this form is plain pipes. `exec 3>&1` rather than a `{ ...; } 3>&1` group:
    # under bash 3.2 the group form leaked the child's stdout into the pipe as well, so
    # every stdout line was duplicated into the log.
    exec 3>&1
    "$python_bin" -m python_acp.cli ${cli_args[@]+"${cli_args[@]}"} 2>&1 1>&3 3>&- |
        tee -a -- "$log_path" >&2
    exit "${PIPESTATUS[0]}"
fi

exec "$python_bin" -m python_acp.cli ${cli_args[@]+"${cli_args[@]}"}
