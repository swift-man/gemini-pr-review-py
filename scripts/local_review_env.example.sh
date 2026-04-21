#!/usr/bin/env bash
# `scripts/local_review_env.sh` 로 복사해서 값을 채우세요. 그 파일은 .gitignore 대상입니다.

# --- GitHub App ---
export GITHUB_APP_ID="123456"
export GITHUB_APP_PRIVATE_KEY_PATH="/absolute/path/to/gemini-review.private-key.pem"
export GITHUB_WEBHOOK_SECRET="change-me-long-random"

# --- Gemini CLI ---
# 최초 1회 설치 및 로그인 (Google OAuth 브라우저 플로우):
#   npm i -g @google/gemini-cli
#   gemini   # 브라우저 로그인 완료 후 ~/.gemini/oauth_creds.json 생성됨
#
# 사용 가능 모델 (상위 순):
#   gemini-2.5-pro, gemini-2.5-flash, gemini-2.5-flash-lite
export GEMINI_MODEL="gemini-2.5-pro"
# Primary 모델이 다음 사유로 실패하면 왼쪽부터 순서대로 재시도:
#   - 용량/레이트: 429, resource_exhausted, rate limit exceeded
#   - preview 미가용: "preview ... not found/unavailable"
#   - 스트림/네트워크 절단: ERR_STREAM_PREMATURE_CLOSE, ECONNRESET, socket hang up
# 비우면 fallback 비활성화:
#   export GEMINI_FALLBACK_MODELS=""
export GEMINI_FALLBACK_MODELS="gemini-2.5-pro"
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
# export DRY_RUN="1"    # 주석 해제하면 PR 에 게시하지 않고 로그만 남깁니다
