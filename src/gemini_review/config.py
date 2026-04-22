from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # GitHub App
    github_app_id: int = Field(..., alias="GITHUB_APP_ID")
    github_app_private_key_path: Path | None = Field(
        default=None, alias="GITHUB_APP_PRIVATE_KEY_PATH"
    )
    github_app_private_key: str | None = Field(default=None, alias="GITHUB_APP_PRIVATE_KEY")
    github_webhook_secret: str = Field(..., alias="GITHUB_WEBHOOK_SECRET")
    github_api_base: str = Field(default="https://api.github.com", alias="GITHUB_API_BASE")

    # Gemini CLI
    gemini_bin: str = Field(default="gemini", alias="GEMINI_BIN")
    gemini_model: str = Field(default="gemini-2.5-pro", alias="GEMINI_MODEL")
    gemini_fallback_models: str = Field(
        default="gemini-2.5-pro",
        alias="GEMINI_FALLBACK_MODELS",
    )
    gemini_timeout_sec: int = Field(default=600, alias="GEMINI_TIMEOUT_SEC")
    # Gemini 2.5-pro 와 3.1-pro-preview 모두 입력 컨텍스트 1,048,576 토큰(1M) 까지.
    # 우리 예산은 이 한도에 chars/token=4 추정 + 프레이밍 오버헤드 여유를 두고
    # 90% 지점인 900,000 으로 설정. 코드 토큰화 비율(0.27~0.32 tokens/char)이 추정보다
    # 높을 때 1M 한도를 넘기지 않기 위한 안전선으로, 이 값을 더 올리는 건 권장하지 않음.
    # 모델이 더 큰 컨텍스트를 지원하게 되면 그 시점에 검토.
    gemini_max_input_tokens: int = Field(default=900_000, alias="GEMINI_MAX_INPUT_TOKENS")
    # Google OAuth 자격 증명 파일 — gemini CLI 의 `gemini auth login` 결과 위치.
    # 기본값은 Gemini CLI 설치 시 생성하는 `~/.gemini/oauth_creds.json`.
    gemini_oauth_creds_path: Path = Field(
        default=Path.home() / ".gemini" / "oauth_creds.json",
        alias="GEMINI_OAUTH_CREDS_PATH",
    )

    # Repo / files
    repo_cache_dir: Path = Field(
        default=Path.home() / ".gemini-review" / "repos", alias="REPO_CACHE_DIR"
    )
    file_max_bytes: int = Field(default=204_800, alias="FILE_MAX_BYTES")
    data_file_max_bytes: int = Field(default=20_000, alias="DATA_FILE_MAX_BYTES")

    # Server
    host: str = Field(default="127.0.0.1", alias="HOST")
    port: int = Field(default=8000, alias="PORT")
    dry_run: bool = Field(default=False, alias="DRY_RUN")
    # 병렬 리뷰 워커 수. 같은 레포에 쌓이는 리뷰는 레포 캐시 디렉터리 락 때문에
    # 어차피 직렬화되지만, 서로 다른 레포의 리뷰는 이 값만큼 동시에 진행한다.
    # OAuth 쿼터가 여유 있다는 전제에서 3이 무난 (동시 `git clone`/`gemini CLI`
    # 프로세스 수 상한이기도 함).
    review_concurrency: int = Field(default=3, alias="REVIEW_CONCURRENCY")

    def load_private_key(self) -> str:
        if self.github_app_private_key:
            return self.github_app_private_key
        if self.github_app_private_key_path:
            return self.github_app_private_key_path.read_text(encoding="utf-8")
        raise RuntimeError(
            "GITHUB_APP_PRIVATE_KEY 또는 GITHUB_APP_PRIVATE_KEY_PATH 중 하나가 필요합니다."
        )

    def parsed_gemini_fallback_models(self) -> tuple[str, ...]:
        return tuple(
            model.strip()
            for model in self.gemini_fallback_models.split(",")
            if model.strip()
        )
