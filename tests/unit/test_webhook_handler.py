import hashlib
import hmac
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from gemini_review.application.review_pr_use_case import ReviewPullRequestUseCase
from gemini_review.application.webhook_handler import WebhookHandler
from gemini_review.domain import (
    FileDump,
    FileEntry,
    PullRequest,
    RepoRef,
    ReviewEvent,
    ReviewResult,
    TokenBudget,
)


SECRET = "top-secret"


@dataclass
class FakeGitHub:
    posted_reviews: list[tuple[PullRequest, ReviewResult]] = field(default_factory=list)
    posted_comments: list[tuple[PullRequest, str]] = field(default_factory=list)
    pr_to_return: PullRequest | None = None

    def fetch_pull_request(
        self, repo: RepoRef, number: int, installation_id: int
    ) -> PullRequest:
        assert self.pr_to_return is not None
        return self.pr_to_return

    def post_review(self, pr: PullRequest, result: ReviewResult) -> None:
        self.posted_reviews.append((pr, result))

    def post_comment(self, pr: PullRequest, body: str) -> None:
        self.posted_comments.append((pr, body))

    def get_installation_token(self, installation_id: int) -> str:
        return "fake-token"


@dataclass
class FakeFetcher:
    path: Path

    def checkout(self, pr: PullRequest, installation_token: str) -> Path:
        return self.path


class FakeCollector:
    def __init__(self, dump: FileDump) -> None:
        self._dump = dump

    def collect(self, root: Path, changed_files: tuple[str, ...], budget: TokenBudget) -> FileDump:
        return self._dump


class FakeEngine:
    def __init__(self, result: ReviewResult) -> None:
        self._result = result

    def review(self, pr: PullRequest, dump: FileDump) -> ReviewResult:
        return self._result


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _sample_pr() -> PullRequest:
    return PullRequest(
        repo=RepoRef("o", "r"),
        number=1,
        title="t",
        body="",
        head_sha="abc",
        head_ref="feat",
        base_sha="def",
        base_ref="main",
        clone_url="https://example/x.git",
        changed_files=("a.py",),
        installation_id=7,
        is_draft=False,
    )


def _build_handler(
    github: FakeGitHub,
    dump: FileDump,
    result: ReviewResult,
    tmp: Path,
) -> WebhookHandler:
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(tmp),
        file_collector=FakeCollector(dump),
        engine=FakeEngine(result),
        max_input_tokens=1000,
    )
    return WebhookHandler(secret=SECRET, github=github, use_case=use_case)


def test_verify_signature_accepts_valid_and_rejects_invalid(tmp_path: Path) -> None:
    dump = FileDump(entries=(), total_chars=0)
    result = ReviewResult(summary="ok", event=ReviewEvent.COMMENT)
    handler = _build_handler(FakeGitHub(), dump, result, tmp_path)

    body = b'{"a":1}'
    assert handler.verify_signature(_sign(body), body) is True
    assert handler.verify_signature("sha256=wrong", body) is False
    assert handler.verify_signature(None, body) is False


def test_accept_ignores_non_pr_events(tmp_path: Path) -> None:
    handler = _build_handler(
        FakeGitHub(),
        FileDump(entries=(), total_chars=0),
        ReviewResult(summary="ok", event=ReviewEvent.COMMENT),
        tmp_path,
    )
    code, _ = handler.accept("issues", "d1", {})
    assert code == 202


def test_accept_rejects_non_dict_payload(tmp_path: Path) -> None:
    """유효 서명 + JSON 이지만 최상위가 배열/프리미티브인 경우 400 으로 조기 실패.

    `payload: object` 로 느슨히 받은 뒤 isinstance 검증으로 좁히는 계약을 고정한다.
    회귀 방지: 이 검증을 잃으면 `payload.get(...)` 에서 AttributeError → 500 으로
    이어져 악의적 입력이 서버 오류로 잡힌다.
    """
    handler = _build_handler(
        FakeGitHub(),
        FileDump(entries=(), total_chars=0),
        ReviewResult(summary="ok", event=ReviewEvent.COMMENT),
        tmp_path,
    )
    for bogus in (["not", "a", "dict"], "string", 42, None):
        code, reason = handler.accept("pull_request", "dbogus", bogus)
        assert code == 400, f"expected 400 for {type(bogus).__name__}, got {code}"
        assert reason == "invalid-payload-shape"


def test_accept_ignores_draft(tmp_path: Path) -> None:
    handler = _build_handler(
        FakeGitHub(),
        FileDump(entries=(), total_chars=0),
        ReviewResult(summary="ok", event=ReviewEvent.COMMENT),
        tmp_path,
    )
    payload: dict[str, Any] = {
        "action": "opened",
        "pull_request": {"draft": True, "number": 1},
        "repository": {"full_name": "o/r"},
        "installation": {"id": 7},
    }
    code, reason = handler.accept("pull_request", "d2", payload)
    assert code == 202
    assert reason == "skipped-draft"


def test_accept_ignores_unsupported_action(tmp_path: Path) -> None:
    handler = _build_handler(
        FakeGitHub(),
        FileDump(entries=(), total_chars=0),
        ReviewResult(summary="ok", event=ReviewEvent.COMMENT),
        tmp_path,
    )
    payload: dict[str, Any] = {
        "action": "closed",
        "pull_request": {"number": 1},
        "repository": {"full_name": "o/r"},
        "installation": {"id": 7},
    }
    code, _ = handler.accept("pull_request", "d3", payload)
    assert code == 202


def test_use_case_posts_comment_when_budget_exceeded(tmp_path: Path) -> None:
    github = FakeGitHub()
    pr = _sample_pr()
    dump = FileDump(
        entries=(),
        total_chars=0,
        excluded=("a.py",),
        exceeded_budget=True,
        budget=TokenBudget(1),
    )
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(tmp_path),
        file_collector=FakeCollector(dump),
        engine=FakeEngine(ReviewResult(summary="x", event=ReviewEvent.COMMENT)),
        max_input_tokens=1,
    )

    use_case.execute(pr)

    assert github.posted_reviews == []
    assert len(github.posted_comments) == 1
    assert "예산 초과" in github.posted_comments[0][1]


def test_use_case_posts_review_when_budget_fits(tmp_path: Path) -> None:
    github = FakeGitHub()
    pr = _sample_pr()
    from gemini_review.domain import FileEntry

    dump = FileDump(
        entries=(FileEntry(path="a.py", content="x=1", size_bytes=3, is_changed=True),),
        total_chars=3,
        exceeded_budget=False,
    )
    expected = ReviewResult(summary="good", event=ReviewEvent.COMMENT)
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(tmp_path),
        file_collector=FakeCollector(dump),
        engine=FakeEngine(expected),
        max_input_tokens=1000,
    )

    use_case.execute(pr)

    assert github.posted_comments == []
    assert len(github.posted_reviews) == 1
    assert github.posted_reviews[0][1] is expected


def test_accept_queues_valid_pr_and_returns_202(tmp_path: Path) -> None:
    handler = _build_handler(
        FakeGitHub(),
        FileDump(entries=(), total_chars=0),
        ReviewResult(summary="ok", event=ReviewEvent.COMMENT),
        tmp_path,
    )
    payload: dict[str, Any] = {
        "action": "opened",
        "pull_request": {"draft": False, "number": 42},
        "repository": {"full_name": "o/r"},
        "installation": {"id": 7},
    }
    code, reason = handler.accept("pull_request", "d4", payload)
    assert code == 202
    assert reason == "queued"


# --- stop() graceful shutdown -----------------------------------------------


class _BlockingEngine:
    """review() 호출 시 `started` 를 세운 뒤 `release` 가 세워질 때까지 블로킹.

    graceful shutdown 타임아웃 분기를 결정적으로 재현하려면 worker 가 현재 작업에
    물려 있는 상태를 테스트에서 정확히 만들어야 한다. sleep 기반 타이밍은 CI 에서
    쉽게 불안정해지므로 Event 로 동기화.
    """

    def __init__(self, result: ReviewResult) -> None:
        self._result = result
        self.started = threading.Event()
        self.release = threading.Event()

    def review(self, pr: PullRequest, dump: FileDump) -> ReviewResult:
        self.started.set()
        self.release.wait(timeout=5.0)
        return self._result


def _build_handler_with_engine(
    tmp_path: Path, engine: _BlockingEngine | FakeEngine, github: FakeGitHub | None = None
) -> WebhookHandler:
    gh = github or FakeGitHub()
    gh.pr_to_return = _sample_pr()
    dump = FileDump(
        entries=(FileEntry(path="a.py", content="x", size_bytes=1, is_changed=True),),
        total_chars=1,
        exceeded_budget=False,
    )
    use_case = ReviewPullRequestUseCase(
        github=gh,
        repo_fetcher=FakeFetcher(tmp_path),
        file_collector=FakeCollector(dump),
        engine=engine,  # type: ignore[arg-type]
        max_input_tokens=1000,
    )
    return WebhookHandler(secret=SECRET, github=gh, use_case=use_case)


def _queued_payload(number: int) -> dict[str, Any]:
    return {
        "action": "opened",
        "pull_request": {"draft": False, "number": number},
        "repository": {"full_name": "o/r"},
        "installation": {"id": 7},
    }


def test_stop_on_unstarted_handler_is_noop(tmp_path: Path) -> None:
    """start() 호출 전에 stop() 해도 예외 없이 바로 반환해야 한다.

    lifespan 이 start 전에 예외로 종료되는 경우, stop 이 join 하려다 NoneType 참조로
    AttributeError 나면 원래 예외를 가려 디버깅이 어려워진다.
    """
    handler = _build_handler(
        FakeGitHub(),
        FileDump(entries=(), total_chars=0),
        ReviewResult(summary="ok", event=ReviewEvent.COMMENT),
        tmp_path,
    )
    handler.stop()  # 예외가 나지 않아야 충분


def test_stop_logs_error_when_worker_is_stuck_past_timeout(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """데몬 워커가 gemini CLI 등에 묶여 timeout 안에 못 끝나면 드롭 사실이 ERROR 로그로 명시돼야 한다.

    회귀 방지 대상: 이 로그를 잃으면 운영자가 "리뷰가 왜 안 달렸는지" 추적할 근거가
    없어져 GitHub 에 202 로 응답한 PR 이 조용히 유실된다 (우선순위 #3).
    """
    engine = _BlockingEngine(ReviewResult(summary="ok", event=ReviewEvent.COMMENT))
    handler = _build_handler_with_engine(tmp_path, engine)

    handler.start()
    try:
        handler.accept("pull_request", "dstuck", _queued_payload(number=7))
        assert engine.started.wait(timeout=3.0), "worker never entered review()"

        with caplog.at_level(logging.ERROR):
            handler.stop(timeout=0.3)

        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert error_records, "타임아웃 시 ERROR 로그가 반드시 찍혀야 한다"
        joined = " | ".join(r.getMessage() for r in error_records)
        assert "worker did not finish" in joined
        # in-flight 작업의 delivery_id 와 PR 식별자가 로그에 포함되어야 재시도 가능
        assert "dstuck" in joined
        assert "o/r#7" in joined
    finally:
        # 데몬 스레드가 프로세스 종료까지 subprocess 안에 매달려 있지 않도록 풀어준다
        engine.release.set()


def test_stop_clears_inflight_after_successful_processing(tmp_path: Path) -> None:
    """정상 처리 종료 후 _in_flight 가 None 으로 정리돼야 stop() 이 드롭 로그를 오탐 안 찍는다."""
    github = FakeGitHub()
    engine = FakeEngine(ReviewResult(summary="ok", event=ReviewEvent.COMMENT))
    handler = _build_handler_with_engine(tmp_path, engine, github=github)

    handler.start()
    try:
        handler.accept("pull_request", "dok", _queued_payload(number=9))
        # 리뷰 게시까지 완료될 때까지 폴링 대기 (sleep 단일값에 의존하지 않음)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not github.posted_reviews:
            time.sleep(0.02)
        assert github.posted_reviews, "review 가 제한 시간 내 게시되지 않았다"

        assert handler._in_flight is None  # type: ignore[attr-defined]
    finally:
        handler.stop(timeout=1.0)
