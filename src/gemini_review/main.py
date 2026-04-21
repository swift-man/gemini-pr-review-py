import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from gemini_review.application.review_pr_use_case import ReviewPullRequestUseCase
from gemini_review.application.webhook_handler import WebhookHandler
from gemini_review.config import Settings
from gemini_review.infrastructure.file_dump_collector import FileDumpCollector
from gemini_review.infrastructure.gemini_cli_engine import GeminiAuthError, GeminiCliEngine
from gemini_review.infrastructure.git_repo_fetcher import GitRepoFetcher
from gemini_review.infrastructure.github_app_client import GitHubAppClient
from gemini_review.logging_utils import configure_logging

logger = logging.getLogger(__name__)


def build_handler(settings: Settings) -> WebhookHandler:
    github = GitHubAppClient(
        app_id=settings.github_app_id,
        private_key_pem=settings.load_private_key(),
        api_base=settings.github_api_base,
        dry_run=settings.dry_run,
    )
    repo_fetcher = GitRepoFetcher(cache_dir=settings.repo_cache_dir)
    collector = FileDumpCollector(
        file_max_bytes=settings.file_max_bytes,
        data_file_max_bytes=settings.data_file_max_bytes,
    )
    engine = GeminiCliEngine(
        binary=settings.gemini_bin,
        model=settings.gemini_model,
        fallback_models=settings.parsed_gemini_fallback_models(),
        timeout_sec=settings.gemini_timeout_sec,
        oauth_creds_path=settings.gemini_oauth_creds_path,
    )

    # 기동 시 Gemini CLI + Google OAuth 자격 상태를 선점검. 토큰이 살아 있으면 로그만
    # 남기고 통과, 없으면 서버 기동 자체를 중단시켜 운영자가 `gemini` 로 브라우저
    # 로그인을 먼저 돌리도록 유도한다.
    try:
        status_line = engine.verify_auth()
        logger.info("gemini auth OK — %s", status_line)
    except GeminiAuthError as exc:
        logger.error("gemini auth preflight failed:\n%s", exc)
        raise

    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=repo_fetcher,
        file_collector=collector,
        engine=engine,
        max_input_tokens=settings.gemini_max_input_tokens,
    )
    return WebhookHandler(
        secret=settings.github_webhook_secret,
        github=github,
        use_case=use_case,
        concurrency=settings.review_concurrency,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging()
    settings = settings or Settings()  # type: ignore[call-arg]
    handler = build_handler(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        handler.start()
        try:
            yield
        finally:
            handler.stop()

    app = FastAPI(title="gemini-review", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhook")
    async def webhook(request: Request) -> Response:
        # 1) raw body 를 먼저 읽는다. 서명 검증은 원문 바이트에 대해서만 유효하므로
        #    json.loads 이후 재직렬화한 값으로 계산하면 안 된다.
        body = await request.body()
        signature = request.headers.get("X-Hub-Signature-256")
        if not handler.verify_signature(signature, body):
            logger.warning("invalid webhook signature")
            return Response(status_code=401, content="invalid signature")

        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return Response(status_code=400, content="invalid json")

        event = request.headers.get("X-GitHub-Event", "")
        delivery = request.headers.get("X-GitHub-Delivery", "-")

        # 2) accept() 는 필터링 후 큐에 넣고 즉시 반환한다. GitHub 는 10초 내 응답이 없으면
        #    webhook 을 실패 처리하므로, 무거운 리뷰 작업은 워커 스레드에서 진행한다.
        status, reason = handler.accept(event, delivery, payload)
        return Response(status_code=status, content=reason)

    return app


def app_factory() -> FastAPI:
    """Uvicorn factory 진입점 — `uvicorn gemini_review.main:app_factory --factory` 로 기동."""
    return create_app()
