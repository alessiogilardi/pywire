#!/usr/bin/env bash
# PostToolUse hook: type-checks the edited .py file with pyright. Type
# errors block the turn (exit 2) so Claude sees and fixes them immediately.
# Files under tests/ get exit 1 instead (visible, non-blocking): TDD's red
# phase legitimately references symbols that don't exist yet, and pyright
# flags those the same way it flags a real typo.
set -uo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/hook_common.sh"
read_hook_target

[[ "$HOOK_FILE_PATH" == *.py ]] || exit 0
[[ -f "$HOOK_FILE_PATH" ]] || exit 0

cd "$HOOK_CWD" || exit 0

command -v uv >/dev/null 2>&1 || report_missing "pyright_check hook: 'uv' not found on PATH — skipped pyright for $HOOK_FILE_PATH"
uv run pyright --version >/dev/null 2>&1 || report_missing "pyright_check hook: 'pyright' not available via 'uv run' — skipped for $HOOK_FILE_PATH"

if ! output="$(uv run pyright "$HOOK_FILE_PATH" 2>&1)"; then
  printf '%s\n' "$output" >&2
  # Normalize backslashes: on Windows, HOOK_FILE_PATH uses native '\' separators.
  normalized_path="$(printf '%s' "$HOOK_FILE_PATH" | tr '\\' '/')"
  [[ "$normalized_path" == */tests/* ]] && exit 1
  exit 2
fi
exit 0
