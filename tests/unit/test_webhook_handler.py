import hashlib
import hmac
import logging
import threading
import time
from collections.abc import Callable
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
        # 테스트가 고정 PR 을 지정하지 않았으면 (repo, number) 에서 합성. 다중 레포 병렬
        # 테스트가 `pr_to_return` 하나로 만족시킬 수 없기 때문에 필요.
        if self.pr_to_return is not None:
            return self.pr_to_return
        return PullRequest(
            repo=repo,
            number=number,
            title=f"t{number}",
            body="",
            head_sha=f"sha{number}",
            head_ref="feat",
            base_sha="base",
            base_ref="main",
            clone_url=f"https://example/{repo.full_name}.git",
            changed_files=("a.py",),
            installation_id=installation_id,
            is_draft=False,
        )

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
    handler.start()
    try:
        payload: dict[str, Any] = {
            "action": "opened",
            "pull_request": {"draft": False, "number": 42},
            "repository": {"full_name": "o/r"},
            "installation": {"id": 7},
        }
        code, reason = handler.accept("pull_request", "d4", payload)
        assert code == 202
        assert reason == "queued"
    finally:
        handler.stop(timeout=2.0)


def test_accept_returns_503_when_handler_not_started(tmp_path: Path) -> None:
    """start() 전에 webhook 이 도착하면 503 으로 거부해야 한다.

    이전(queue.Queue 모델) 은 start 없이도 put 이 동작해 조용히 유실됐다. executor
    모델에선 명시적으로 503 을 주어 GitHub Recent Deliveries 에 실패로 기록되고,
    운영자가 lifespan 기동 실패를 빠르게 인지 가능.
    """
    handler = _build_handler(
        FakeGitHub(),
        FileDump(entries=(), total_chars=0),
        ReviewResult(summary="ok", event=ReviewEvent.COMMENT),
        tmp_path,
    )
    payload: dict[str, Any] = {
        "action": "opened",
        "pull_request": {"draft": False, "number": 1},
        "repository": {"full_name": "o/r"},
        "installation": {"id": 7},
    }
    code, reason = handler.accept("pull_request", "dnot", payload)
    assert code == 503
    assert reason == "not-running"


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
        # 워커가 stuck 상태인 동안 추가로 큐에 쌓이는 작업들 — 드롭 로그에 식별자가 모두
        # 나열되는지 검증하기 위한 고정 케이스.
        handler.accept("pull_request", "dqueued1", _queued_payload(number=11))
        handler.accept("pull_request", "dqueued2", _queued_payload(number=12))

        with caplog.at_level(logging.ERROR):
            handler.stop(timeout=0.3)

        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert error_records, "타임아웃 시 ERROR 로그가 반드시 찍혀야 한다"
        joined = " | ".join(r.getMessage() for r in error_records)
        assert "worker did not finish" in joined
        # in-flight 작업의 delivery_id 와 PR 식별자가 로그에 포함되어야 재시도 가능
        assert "dstuck" in joined
        assert "o/r#7" in joined
        # 큐에 남아 있던 작업들의 식별자도 로그에 포함 — 운영자가 "몇 개" 가 아니라
        # "어떤 PR" 들을 재시도해야 하는지 알아야 한다.
        assert "dqueued1" in joined
        assert "o/r#11" in joined
        assert "dqueued2" in joined
        assert "o/r#12" in joined
    finally:
        # 데몬 스레드가 프로세스 종료까지 subprocess 안에 매달려 있지 않도록 풀어준다
        engine.release.set()


def test_stop_allows_restart_after_clean_shutdown(tmp_path: Path) -> None:
    """정상 종료 후 같은 핸들러 인스턴스에서 start() 가 다시 동작해야 한다.

    회귀 방지: stop() 이 `_executor` 레퍼런스를 지우지 않으면 `start()` 의
    `if self._executor is not None: return` 에 막혀 재기동이 조용히 실패한다.
    lifespan 이 한 번만 돌더라도, 테스트·개발 시 같은 인스턴스를 재사용할 때
    좀비 레퍼런스로 인해 워커가 뜨지 않는 혼란을 막는다.
    """
    handler = _build_handler(
        FakeGitHub(),
        FileDump(entries=(), total_chars=0),
        ReviewResult(summary="ok", event=ReviewEvent.COMMENT),
        tmp_path,
    )

    handler.start()
    first_executor = handler._executor  # type: ignore[attr-defined]
    assert first_executor is not None

    handler.stop(timeout=1.0)
    assert handler._executor is None  # type: ignore[attr-defined]

    handler.start()
    second_executor = handler._executor  # type: ignore[attr-defined]
    assert second_executor is not None
    assert second_executor is not first_executor
    handler.stop(timeout=1.0)


def test_stop_clears_inflight_after_successful_processing(tmp_path: Path) -> None:
    """정상 처리 종료 후 _in_flight 가 비어야 stop() 이 드롭 로그를 오탐 안 찍는다.

    경쟁 조건 주의: `FakeGitHub.post_review` 가 `posted_reviews` 에 append 된 **직후**
    에도 `_process` 의 `finally:` 블록은 아직 실행되지 않았을 수 있다. 즉
    `posted_reviews` 만 보고 assert 하면 finally 직전의 찰나를 잡아 간헐적으로 실패
    한다. 폴링 조건에 `not _in_flight` 까지 AND 로 묶어 finally 가 돌 때까지 기다린다.
    """
    github = FakeGitHub()
    engine = FakeEngine(ReviewResult(summary="ok", event=ReviewEvent.COMMENT))
    handler = _build_handler_with_engine(tmp_path, engine, github=github)

    handler.start()
    try:
        handler.accept("pull_request", "dok", _queued_payload(number=9))
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if github.posted_reviews and not handler._in_flight:  # type: ignore[attr-defined]
                break
            time.sleep(0.02)
        else:
            pytest.fail("review + in-flight 정리가 제한 시간 내 완료되지 않았다")

        assert github.posted_reviews
        assert not handler._in_flight  # type: ignore[attr-defined]
    finally:
        handler.stop(timeout=1.0)


# --- 병렬 처리 정책 검증 ----------------------------------------------------


class _RecordingBlockingEngine:
    """review() 진입을 (repo, number) 키로 기록하고, 각 키별 release 이벤트를 대기한다.

    병렬/직렬 동작을 실측하려면 "어떤 review 가 언제 engine 안에 들어왔는가" 를 관찰할
    수 있어야 한다. sleep 기반 추정보다 이 방식이 결정적이라 CI 가 안정적.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.entered: list[tuple[str, int]] = []
        self._release: dict[tuple[str, int], threading.Event] = {}

    def _event_for(self, key: tuple[str, int]) -> threading.Event:
        with self._lock:
            return self._release.setdefault(key, threading.Event())

    def review(self, pr: PullRequest, dump: FileDump) -> ReviewResult:
        key = (pr.repo.full_name, pr.number)
        with self._lock:
            self.entered.append(key)
        self._event_for(key).wait(timeout=5.0)
        return ReviewResult(summary="ok", event=ReviewEvent.COMMENT)

    def release(self, repo_full: str, number: int) -> None:
        self._event_for((repo_full, number)).set()

    def snapshot_entered(self) -> list[tuple[str, int]]:
        with self._lock:
            return list(self.entered)


def _build_handler_for_parallel_test(
    tmp_path: Path, engine: _RecordingBlockingEngine, concurrency: int
) -> tuple[WebhookHandler, FakeGitHub]:
    github = FakeGitHub()  # pr_to_return=None → 요청별로 합성
    dump = FileDump(
        entries=(FileEntry(path="a.py", content="x", size_bytes=1, is_changed=True),),
        total_chars=1,
        exceeded_budget=False,
    )
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(tmp_path),
        file_collector=FakeCollector(dump),
        engine=engine,  # type: ignore[arg-type]
        max_input_tokens=1000,
    )
    handler = WebhookHandler(
        secret=SECRET, github=github, use_case=use_case, concurrency=concurrency
    )
    return handler, github


def _payload_for(repo_full: str, number: int) -> dict[str, Any]:
    return {
        "action": "opened",
        "pull_request": {"draft": False, "number": number},
        "repository": {"full_name": repo_full},
        "installation": {"id": 7},
    }


def _wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_reviews_on_different_repos_run_in_parallel(tmp_path: Path) -> None:
    """서로 다른 레포의 리뷰는 concurrency 만큼 동시에 실행돼야 한다.

    회귀 방지: per-repo 락이 실수로 전역 락으로 바뀌거나, executor 가 1개 워커로
    fallback 되면 이 테스트가 실패한다.
    """
    engine = _RecordingBlockingEngine()
    handler, _ = _build_handler_for_parallel_test(tmp_path, engine, concurrency=3)

    handler.start()
    try:
        handler.accept("pull_request", "d1", _payload_for("o/r1", 1))
        handler.accept("pull_request", "d2", _payload_for("o/r2", 2))

        # 첫 리뷰를 release 하지 않은 상태에서 둘 다 engine 에 진입해야 "병렬" 이다.
        assert _wait_until(lambda: len(engine.snapshot_entered()) >= 2), (
            "2건이 제한 시간 내 engine 에 진입하지 않음 — 병렬 실행되지 않는 것"
        )
        entered = engine.snapshot_entered()
        assert {entered[0][0], entered[1][0]} == {"o/r1", "o/r2"}
    finally:
        for repo, number in engine.snapshot_entered():
            engine.release(repo, number)
        handler.stop(timeout=2.0)


def test_reviews_on_same_repo_are_serialized(tmp_path: Path) -> None:
    """같은 레포의 리뷰는 repo 락으로 직렬화 — 두 번째 리뷰는 첫 번째가 끝나야 시작한다.

    회귀 방지: per-repo 락이 빠지거나 잘못 생성되면 같은 레포의 git 캐시 디렉터리를
    동시에 건드려 clone/checkout 경합이 발생한다.
    """
    engine = _RecordingBlockingEngine()
    handler, _ = _build_handler_for_parallel_test(tmp_path, engine, concurrency=3)

    handler.start()
    try:
        handler.accept("pull_request", "d1", _payload_for("o/same", 1))
        handler.accept("pull_request", "d2", _payload_for("o/same", 2))

        # 첫 리뷰만 진입해야 한다 (두 번째는 repo 락 대기).
        assert _wait_until(lambda: len(engine.snapshot_entered()) >= 1)
        time.sleep(0.15)  # 두 번째가 "실수로" 진입할 시간 여유
        assert engine.snapshot_entered() == [("o/same", 1)], (
            "두 번째 리뷰가 첫 번째 완료 전에 engine 에 진입 — 직렬화 실패"
        )

        # 첫 번째 release → 두 번째가 들어와야.
        engine.release("o/same", 1)
        assert _wait_until(lambda: len(engine.snapshot_entered()) >= 2)
        assert engine.snapshot_entered()[1] == ("o/same", 2)

        engine.release("o/same", 2)
    finally:
        # 안전망: 혹시 남아 있는 event 를 풀어 주기
        for repo, number in engine.snapshot_entered():
            engine.release(repo, number)
        handler.stop(timeout=2.0)


def test_parallel_different_repos_while_serializing_same_repo(tmp_path: Path) -> None:
    """혼합 시나리오: o/a#1, o/a#2, o/b#1 이 동시에 도착했을 때
    o/a 쪽 두 건은 직렬화되지만 o/b#1 은 즉시 병렬 실행된다.
    """
    engine = _RecordingBlockingEngine()
    handler, _ = _build_handler_for_parallel_test(tmp_path, engine, concurrency=3)

    handler.start()
    try:
        handler.accept("pull_request", "da1", _payload_for("o/a", 1))
        handler.accept("pull_request", "da2", _payload_for("o/a", 2))
        handler.accept("pull_request", "db1", _payload_for("o/b", 1))

        # o/a#1 과 o/b#1 이 각자의 worker 에서 병렬 진입.
        assert _wait_until(lambda: len(engine.snapshot_entered()) >= 2)
        time.sleep(0.15)
        entered = engine.snapshot_entered()
        # o/a#2 는 아직 대기 상태여야 한다.
        assert ("o/a", 2) not in entered
        assert ("o/a", 1) in entered
        assert ("o/b", 1) in entered

        # o/a#1 끝내면 o/a#2 가 들어간다.
        engine.release("o/a", 1)
        assert _wait_until(lambda: ("o/a", 2) in engine.snapshot_entered())

        engine.release("o/a", 2)
        engine.release("o/b", 1)
    finally:
        for repo, number in engine.snapshot_entered():
            engine.release(repo, number)
        handler.stop(timeout=2.0)
