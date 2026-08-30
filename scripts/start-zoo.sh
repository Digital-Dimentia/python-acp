#!/usr/bin/env bash
#
# Start python-acp on stdio with the mock server's **schema zoo** switched on, for
# looking at `AvailableCommand._meta` against something worth rendering.
#
# The whole job is one exported variable. `MOCK_MCP_SCHEMA_ZOO=1` is read by
# `tests/fixtures/mock_mcp_server.py`, not by this agent — but a session's MCP servers are
# spawned as *children* of this process and inherit its environment, so exporting it here
# means a client's `session/new` can name the fixture with a plain `"env": []` and still
# get the zoo. That is the difference between a paste-able two-line server entry and one
# every client has to remember to decorate.
#
# See docs/tool-schema-contract.md for what is in `_meta` and why, and
# tests/fixtures/mock_mcp_server.py for the thirteen tools this turns on.

set -euo pipefail

readonly REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
    cat <<'USAGE'
Usage: scripts/start-zoo.sh [--log[=PATH]] [-- ] [cli args...]

Starts python-acp on `--transport stdio` with MOCK_MCP_SCHEMA_ZOO=1 exported, so an
MCP server spawned from `session/new` serves the schema zoo without the client
having to pass the variable itself.

ACP speaks on stdin/stdout; every diagnostic, this banner included, is on stderr.

Options:
  --log[=PATH]   Passed to scripts/start-ws.sh: also append diagnostics to PATH
                 (bare --log writes to logs/python-acp-ws.log). stderr only, so
                 stdout stays a clean protocol wire.
  -h, --help     This message.

Every other argument is forwarded to the CLI. `--transport stdio` is prepended and
a later `--transport` would override it, which is deliberate: nothing here is
specific to stdio except the default.

Environment:
  VENV_DIR       Virtual environment to activate (default: .venv).
  MOCK_MCP_SCHEMA_ZOO
                 Forced to 1. A script named start-zoo that honoured an inherited
                 0 would be a trap.
  MOCK_MCP_ANNOTATED_TOOLS
                 Not set here, but inherited if you export it — the fixture's
                 other opt-in, adding a destructive, an additive, and an
                 unannotated tool for the permission-kind mapping.

Examples:
  scripts/start-zoo.sh
  scripts/start-zoo.sh --log --debug
USAGE
}

for arg in "$@"; do
    case "$arg" in
        -h | --help)
            usage
            exit 0
            ;;
        --)
            break
            ;;
    esac
done

# Forced, not defaulted. See the help text: an inherited 0 would leave a script called
# start-zoo serving one `echo` tool, which is the confusing half of both worlds.
export MOCK_MCP_SCHEMA_ZOO=1

# Never fd 1: under `--transport stdio` that is the wire, and a banner on it corrupts the
# first message. Same rule the launcher below follows for its own diagnostics.
cat >&2 <<BANNER
Starting python-acp (stdio) with the schema zoo (MOCK_MCP_SCHEMA_ZOO=1).
ACP on stdin/stdout; diagnostics on stderr. Ctrl+C (or EOF) to stop.

Name the fixture in session/new — \`env\` can stay empty, this process exports it:

  {"jsonrpc":"2.0","id":2,"method":"session/new","params":{
    "cwd":"${REPO_ROOT}",
    "mcpServers":[{"name":"zoo","command":"python",
                   "args":["tests/fixtures/mock_mcp_server.py"],"env":[]}]}}

Then, to stop being asked before every call:

  {"jsonrpc":"2.0","id":3,"method":"session/set_mode","params":{
    "sessionId":"<id>","modeId":"auto-approve"}}

Thirteen zoo tools are announced as \`zoo/zoo-*\`, each carrying its inputSchema on
AvailableCommand._meta["python-acp/tool"]. Try:

  /zoo/zoo-choices --colour red --priority P1 --retries 3
BANNER

# Everything else — venv activation, the empty-argument rule, --log, keeping stdout
# clean — already lives in start-ws.sh, which is transport-agnostic by construction.
# Duplicating any of it here would give this script its own copy to drift.
#
# `${@+"$@"}`: macOS ships bash 3.2, where an empty "$@" counts as unbound under `set -u`.
exec "${REPO_ROOT}/scripts/start-ws.sh" --transport stdio ${@+"$@"}
