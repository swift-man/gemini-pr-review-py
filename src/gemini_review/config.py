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
    gemini_timeout_sec: int = Field(default=600, alias="GEMINI_TIMEOUT_SEC")
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

    def load_private_key(self) -> str:
        if self.github_app_private_key:
            return self.github_app_private_key
        if self.github_app_private_key_path:
            return self.github_app_private_key_path.read_text(encoding="utf-8")
        raise RuntimeError(
            "GITHUB_APP_PRIVATE_KEY 또는 GITHUB_APP_PRIVATE_KEY_PATH 중 하나가 필요합니다."
        )
