#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

export PATH="/Library/Frameworks/Python.framework/Versions/Current/bin:/opt/homebrew/bin:/usr/local/bin:${HOME}/.orbstack/bin:${PATH}"

if [[ -x "${REPO_ROOT}/.venv/bin/python3" ]]; then
  exec "${REPO_ROOT}/.venv/bin/python3" -m tools.admin_console "$@"
fi
exec python3 -m tools.admin_console "$@"
