#!/usr/bin/env bash
# PostToolUse hook: silently autofixes the edited .py file with ruff
# (check --fix + format). Never blocks the turn.
set -uo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/hook_common.sh"
read_hook_target

[[ "$HOOK_FILE_PATH" == *.py ]] || exit 0
[[ -f "$HOOK_FILE_PATH" ]] || exit 0

cd "$HOOK_CWD" || exit 0

command -v uv >/dev/null 2>&1 || report_missing "ruff_autofix hook: 'uv' not found on PATH — skipped ruff for $HOOK_FILE_PATH"
uv run ruff --version >/dev/null 2>&1 || report_missing "ruff_autofix hook: 'ruff' not available via 'uv run' — skipped for $HOOK_FILE_PATH"

uv run ruff check --fix "$HOOK_FILE_PATH" >/dev/null 2>&1
uv run ruff format "$HOOK_FILE_PATH" >/dev/null 2>&1
exit 0
