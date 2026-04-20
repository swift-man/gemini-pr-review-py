#!/usr/bin/env bash
# Copy to `scripts/local_review_env.sh` and fill in. That file is gitignored.

# --- GitHub App ---
export GITHUB_APP_ID="123456"
export GITHUB_APP_PRIVATE_KEY_PATH="/absolute/path/to/gemini-review.private-key.pem"
export GITHUB_WEBHOOK_SECRET="change-me-long-random"

# --- Gemini CLI ---
# Install & login once (Google OAuth browser flow):
#   npm i -g @google/gemini-cli
#   gemini   # follow the browser sign-in, creds saved to ~/.gemini/oauth_creds.json
#
# Available models (상위 순):
#   gemini-2.5-pro, gemini-2.5-flash, gemini-2.5-flash-lite
export GEMINI_MODEL="gemini-2.5-pro"
export GEMINI_MAX_INPUT_TOKENS="900000"
export GEMINI_TIMEOUT_SEC="600"
# Homebrew / nvm 경로가 PATH 에 없는 daemon 환경이면 절대 경로로 고정:
#   export GEMINI_BIN="/opt/homebrew/bin/gemini"
#   export GEMINI_BIN="$HOME/.nvm/versions/node/v20.11.1/bin/gemini"
# export GEMINI_OAUTH_CREDS_PATH="$HOME/.gemini/oauth_creds.json"

# --- Repo cache / files ---
export REPO_CACHE_DIR="$HOME/.gemini-review/repos"
export FILE_MAX_BYTES="204800"
# JSON/YAML/XML 같은 모호한 확장자에 대한 더 엄격한 상한 (설정/매니페스트 이름은 예외로 항상 포함).
export DATA_FILE_MAX_BYTES="20000"

# --- Server ---
export HOST="127.0.0.1"
export PORT="8000"
# export DRY_RUN="1"    # uncomment to log reviews without posting
