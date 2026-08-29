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
                 clean protocol wire. The file is opened before the venv is
                 checked, so a launch that never happens is recorded too.
  -h, --help     This message.

Every other argument is passed through to the CLI untouched, so the usual flags
work: --host, --port, --debug, --transport. The one exception is the empty
string, which is dropped: it is never meaningful to the CLI, and forwarding it
turns a blank row in a caller's config into an unexplained exit 2.

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
            # Still filtered: `--` ends option parsing, not the empty-argument rule.
            while (($# > 0)); do
                [[ -n $1 ]] && cli_args+=("$1")
                shift
            done
            break
            ;;
        *)
            # Drop the empty string rather than forwarding it. argparse rejects it as an
            # `unrecognized arguments:` with nothing after the colon, so the one line on
            # screen names no cause and the exit is a bare 2. A client that builds its
            # command line by joining a list — acp-ui does, in agent.rs, then hands the
            # string to `$SHELL -l -c` — turns a blank row in a config form into exactly
            # that. An empty argument is never meaningful to the CLI, so dropping it
            # costs nothing and keeps the pass-through honest for everything else.
            [[ -n $1 ]] && cli_args+=("$1")
            shift
            ;;
    esac
done

# Open the log *before* anything that can fail, so every path from here down is recorded.
# The venv check and `source activate` below both used to run ahead of the banner, so a
# missing interpreter or a broken activate spoke on stderr alone: a client that swallows
# stderr — as acp-ui does — showed the operator an empty log file and a bare "agent
# process exited", which reads as a mid-session crash rather than a launch that never
# happened. The log is the operator's one durable record; anything that can kill this
# script has to reach it.
if [[ -n $log_path ]]; then
    log_dir="$(dirname -- "$log_path")"
    if ! mkdir -p -- "$log_dir"; then
        printf '%s: cannot create log directory %s\n' "${BASH_SOURCE[0]}" "$log_dir" >&2
        exit 1
    fi
    # A banner per run, because the file is appended to: without it two runs read as
    # one session with an inexplicable rebind in the middle.
    if ! printf -- '--- python-acp start %s ---\n' \
        "$(date '+%Y-%m-%dT%H:%M:%S%z')" >>"$log_path"; then
        printf '%s: cannot write log file %s\n' "${BASH_SOURCE[0]}" "$log_path" >&2
        exit 1
    fi
    printf 'Logging to %s\n' "$log_path" >&2
fi

# Every pre-launch diagnostic goes through here: stderr for whoever is watching, and the
# log for whoever is not. Never fd 1 — under `--transport stdio` that is the wire.
diagnose() {
    printf '%s: %s\n' "${BASH_SOURCE[0]}" "$*" >&2
    if [[ -n $log_path ]]; then
        printf '%s: %s\n' "${BASH_SOURCE[0]}" "$*" >>"$log_path"
    fi
}

readonly venv_dir="${VENV_DIR:-${REPO_ROOT}/.venv}"
readonly python_bin="${venv_dir}/bin/python"

if [[ ! -x $python_bin ]]; then
    diagnose "no interpreter at ${python_bin} -- run \`make venv\` first."
    exit 1
fi

# Activate, rather than just calling the venv's interpreter by path. The two are the
# same for *this* process, but not for its children: activation exports VIRTUAL_ENV and
# puts $venv_dir/bin at the front of PATH, so an MCP server that a client names in
# `session/new` as a bare `python` resolves to this venv instead of whatever interpreter
# happened to be on the caller's PATH.
#
# Guarded rather than left to `set -e`, which would abort with whatever status the
# activate script last produced and no explanation anywhere.
# shellcheck disable=SC1091  # resolved at runtime, not a checked-in file
source "${venv_dir}/bin/activate" && activate_status=0 || activate_status=$?
if ((activate_status != 0)); then
    diagnose "failed to activate ${venv_dir} (exit ${activate_status})"
    exit "$activate_status"
fi

# Keep log lines timely when stderr is a pipe rather than a terminal.
export PYTHONUNBUFFERED=1

cd -- "$REPO_ROOT"

# Both invocations below expand argv as `${cli_args[@]+"${cli_args[@]}"}` rather than a
# plain `"${cli_args[@]}"`: macOS ships bash 3.2, where an empty array counts as unbound
# under `set -u`, so a no-argument run would abort before starting anything.

if [[ -n $log_path ]]; then
    # The banner and the log directory are already in place — see above.
    #
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
