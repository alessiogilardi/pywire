# Shared helpers for PostToolUse Edit/Write hooks. Sourced, not executed directly.

extract_json_string() {
  local key="$1" re
  re="\"${key}\"[[:space:]]*:[[:space:]]*\"([^\"]*)\""
  if [[ "$HOOK_PAYLOAD" =~ $re ]]; then
    printf '%s' "${BASH_REMATCH[1]}"
  fi
}

unescape_backslashes() {
  local s="$1"
  printf '%s' "${s//\\\\/\\}"
}

# Reads the hook's stdin JSON once and sets HOOK_FILE_PATH / HOOK_CWD.
read_hook_target() {
  HOOK_PAYLOAD="$(cat)"
  HOOK_FILE_PATH="$(unescape_backslashes "$(extract_json_string file_path)")"
  HOOK_CWD="$(unescape_backslashes "$(extract_json_string cwd)")"
}

# Reports a missing dependency without blocking the turn (exit 1: visible, non-blocking).
report_missing() {
  echo "$1" >&2
  exit 1
}
