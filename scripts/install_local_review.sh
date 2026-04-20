#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

PY_BIN="${PY_BIN:-python3}"
VENV_DIR="${VENV_DIR:-$ROOT/.venv}"

if [[ ! -d "$VENV_DIR" ]]; then
  "$PY_BIN" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet -e "$ROOT[dev]"

echo "Installed gemini-review into $VENV_DIR"
echo "Next: cp scripts/local_review_env.example.sh scripts/local_review_env.sh && edit."
echo "Also required: install gemini CLI (e.g. \`npm i -g @google/gemini-cli\`) and run"
echo "  gemini"
echo "once to complete the Google OAuth browser login."
