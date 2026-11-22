#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "${REPO_ROOT}"

export PATH="/Library/Frameworks/Python.framework/Versions/Current/bin:/opt/homebrew/bin:/usr/local/bin:${HOME}/.orbstack/bin:${PATH}"

PY_BIN=""
if [[ -x "${REPO_ROOT}/.venv/bin/python3" ]]; then
  PY_BIN="${REPO_ROOT}/.venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
  PY_BIN="$(command -v python3)"
else
  echo "[错误] 未找到 python3 运行环境，请先安装 Python 3。"
  read -r -p "按回车键退出..."
  exit 1
fi

exec "${PY_BIN}" -m tools.admin_console "$@"
