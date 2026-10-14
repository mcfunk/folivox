#!/bin/sh
# Docker secrets arrive as files; Folivox reads API keys from the environment (there is no OS keychain in a
# container). Export every /run/secrets/<NAME> whose name looks like a variable (OPENAI_API_KEY, ...), unless
# that variable is already set. The studio token is read by Folivox itself from FOLIVOX_UI_TOKEN_FILE.
set -eu
if [ -d /run/secrets ]; then
  for f in /run/secrets/*; do
    [ -f "$f" ] || continue
    name=$(basename "$f")
    case "$name" in *[!A-Z0-9_]*|[0-9]*) continue ;; esac
    eval "current=\${$name:-}"
    [ -n "$current" ] || export "$name=$(cat "$f")"
  done
fi
[ -z "${FOLIVOX_UI_TOKEN:-}" ] && [ -z "${FOLIVOX_UI_TOKEN_FILE:-}" ] && [ -f /run/secrets/folivox_ui_token ] \
  && export FOLIVOX_UI_TOKEN_FILE=/run/secrets/folivox_ui_token
exec folivox "$@"
