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
    PostedReviewComment,
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

    def list_self_review_comments(
        self, pr: PullRequest
    ) -> tuple[PostedReviewComment, ...]:
        # 웹훅 흐름 테스트는 dedup/follow-up 동작과 무관 — 항상 빈 history 반환.
        # CrossPrFindingDeduper / DiffBasedResolutionChecker 의 진짜 동작은 별도 단위
        # 테스트에서 검증.
        return ()

    def reply_to_review_comment(
        self, pr: PullRequest, comment_id: int, body: str
    ) -> None:
        # 웹훅 흐름 테스트는 follow-up reply 동작과 무관 — no-op.
        # DiffBasedResolutionChecker 의 진짜 동작은 별도 단위 테스트에서 검증.
        return

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
        # diff fallback 진입 시 사용된 인자 기록 — 테스트 검증용
        self.diff_calls: list[tuple[PullRequest, str]] = []

    def review(self, pr: PullRequest, dump: FileDump) -> ReviewResult:
        return self._result

    def review_diff(self, pr: PullRequest, diff_text: str) -> ReviewResult:
        # diff fallback 흐름 테스트가 결과를 일반 review 와 구분할 수 있도록 새 객체로 반환.
        # 호출 인자도 함께 기록해 fallback 진입 자체와 입력 내용을 검증 가능.
        self.diff_calls.append((pr, diff_text))
        return self._result


class FakeFindingVerifier:
    """기본은 finding 을 그대로 반환 — 웹훅 흐름 테스트는 phantom-quote 검증 로직과 무관.

    SourceGroundedFindingVerifier 의 진짜 동작은 별도 단위 테스트 (`test_source_grounded_finding_verifier.py`)
    에서 검증. 여기서는 use case 가 verifier 를 호출하기만 하면 됨.
    """

    def verify(self, result: ReviewResult, repo_root: Path) -> ReviewResult:
        return result


class FakeFindingDeduper:
    """기본은 finding 을 그대로 반환 — 웹훅 흐름 테스트는 cross-PR dedup 로직과 무관.

    CrossPrFindingDeduper 의 진짜 동작은 별도 단위 테스트 (`test_cross_pr_finding_deduper.py`)
    에서 검증. 여기서는 use case 가 deduper 를 호출하기만 하면 됨.
    """

    def dedupe(self, result: ReviewResult, pr: PullRequest) -> ReviewResult:
        return result


class FakeFindingResolutionChecker:
    """no-op — 웹훅 흐름 테스트는 follow-up reply 로직과 무관.

    DiffBasedResolutionChecker 의 진짜 동작은 별도 단위 테스트
    (`test_diff_based_resolution_checker.py`) 에서 검증.
    """

    def check_resolutions(self, pr: PullRequest, repo_root: Path) -> None:
        return


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
        finding_verifier=FakeFindingVerifier(),
        finding_deduper=FakeFindingDeduper(),
        resolution_checker=FakeFindingResolutionChecker(),
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


def test_use_case_posts_comment_when_budget_exceeded_and_no_diff_available(
    tmp_path: Path,
) -> None:
    """예산 초과 + 변경 파일이 binary/truncate 라 patch 가 없으면 → notice 게시.

    회귀 방지: diff fallback 이 도입돼도 patch 가 없는 경우는 그대로 notice 경로 유지.
    `_sample_pr()` 가 `file_patches=()` 라 fallback 이 빈 diff 를 반환 → notice fall-through.
    """
    github = FakeGitHub()
    pr = _sample_pr()  # file_patches=() default
    dump = FileDump(
        entries=(),
        total_chars=0,
        excluded=("a.py",),
        budget_excluded=("a.py",),  # codex PR #26 review #6: budget cut 직접 명시
        exceeded_budget=True,
        budget=TokenBudget(1),
    )
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(tmp_path),
        file_collector=FakeCollector(dump),
        engine=FakeEngine(ReviewResult(summary="x", event=ReviewEvent.COMMENT)),
        finding_verifier=FakeFindingVerifier(),
        finding_deduper=FakeFindingDeduper(),
        resolution_checker=FakeFindingResolutionChecker(),
        max_input_tokens=1,
    )

    use_case.execute(pr)

    assert github.posted_reviews == []
    assert len(github.posted_comments) == 1
    assert "예산 초과" in github.posted_comments[0][1]


def test_use_case_runs_resolution_check_even_when_budget_fallback_aborts(
    tmp_path: Path,
) -> None:
    """예산 초과로 새 리뷰 못 남기는 경우에도 prior 코멘트의 resolution check 는 실행돼야.

    회귀 방지 (gemini PR #28 review #2): 이전 구현은 `result is None` 일 때 일찍
    `return` → `check_resolutions` skip. 메인테이너가 이전 push 의 코멘트 라인을 이미
    수정했을 수 있는데 새 리뷰 게시 실패와 맞물려 follow-up 추적까지 잃는 회귀.
    이제는 try/finally 로 보장 — 새 리뷰 못 남겨도 prior 코멘트 추적은 계속.
    """

    class _RecordingResolutionChecker:
        def __init__(self) -> None:
            self.call_count = 0

        def check_resolutions(self, pr: PullRequest, repo_root: Path) -> None:
            self.call_count += 1

    github = FakeGitHub()
    pr = _sample_pr()  # file_patches=() default → diff fallback 도 빈 diff 로 실패
    dump = FileDump(
        entries=(),
        total_chars=0,
        excluded=("a.py",),
        budget_excluded=("a.py",),
        exceeded_budget=True,
        budget=TokenBudget(1),
    )
    resolver = _RecordingResolutionChecker()
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(tmp_path),
        file_collector=FakeCollector(dump),
        engine=FakeEngine(ReviewResult(summary="x", event=ReviewEvent.COMMENT)),
        finding_verifier=FakeFindingVerifier(),
        finding_deduper=FakeFindingDeduper(),
        resolution_checker=resolver,
        max_input_tokens=1,
    )

    use_case.execute(pr)

    # 새 리뷰는 안 게시됐지만 (notice 만)...
    assert github.posted_reviews == []
    assert len(github.posted_comments) == 1
    # prior 코멘트 resolution check 는 여전히 실행돼야 — try/finally 보장
    assert resolver.call_count == 1, (
        "budget exceeded + diff fallback 실패해도 resolution check 는 호출돼야"
    )


def test_use_case_falls_back_to_diff_review_when_budget_exceeded_with_patches(
    tmp_path: Path,
) -> None:
    """예산 초과 + 변경 파일 patch 존재 → diff fallback 으로 리뷰 수행 + post_review 게시.

    핵심 회귀 방지 (사용자 신고): 큰 저장소에서 GEMINI_MAX_INPUT_TOKENS 초과 시 이전엔
    리뷰 자체를 skip 하고 notice 만 남겼는데, 이제는 diff 만으로라도 리뷰를 수행해
    "0건" 보다 "narrower 리뷰" 를 사용자에게 제공.
    """
    github = FakeGitHub()
    pr = PullRequest(
        repo=RepoRef("o", "r"),
        number=42,
        title="대형 PR",
        body="",
        head_sha="abc",
        head_ref="feat",
        base_sha="def",
        base_ref="main",
        clone_url="https://example/x.git",
        changed_files=("a.py",),
        installation_id=7,
        is_draft=False,
        file_patches=(("a.py", "@@ -1,1 +1,2 @@\n a\n+B\n"),),
        # _fetch_files_for_pr 가 같은 /files 응답에서 둘 다 채우는 흐름 모사
        # (gemini PR #26 review #7: assemble_pr_diff 가 캐시 lookup).
        addable_lines=(("a.py", frozenset({1, 2})),),
    )
    # 예산은 SYSTEM_RULES (~5KB) + DIFF_MODE_NOTICE + PR 메타 + diff 본문 합쳐 들어갈
    # 만큼. 50000 tokens = 200000 chars 로 작은 diff 테스트는 여유롭게 통과.
    dump = FileDump(
        entries=(),
        total_chars=0,
        excluded=("a.py",),
        budget_excluded=("a.py",),  # codex PR #26 review #6: budget cut 직접 명시
        exceeded_budget=True,
        budget=TokenBudget(50000),
    )
    expected = ReviewResult(summary="diff-review", event=ReviewEvent.COMMENT)
    engine = FakeEngine(expected)
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(tmp_path),
        file_collector=FakeCollector(dump),
        engine=engine,
        finding_verifier=FakeFindingVerifier(),
        finding_deduper=FakeFindingDeduper(),
        resolution_checker=FakeFindingResolutionChecker(),
        max_input_tokens=50000,
    )

    use_case.execute(pr)

    assert len(engine.diff_calls) == 1, "engine.review_diff 가 호출돼야"
    assert engine.diff_calls[0][0] is pr
    assert "a.py" in engine.diff_calls[0][1], "diff_text 에 변경 파일 경로 포함돼야"
    assert "+B" in engine.diff_calls[0][1], "diff_text 에 추가 라인 포함돼야"
    assert github.posted_comments == [], "diff fallback 에선 notice 게시 안 함"
    assert len(github.posted_reviews) == 1
    assert github.posted_reviews[0][1] is expected, "diff review 결과가 그대로 게시돼야"


def test_use_case_does_not_fallback_when_changed_file_is_deleted_from_disk(
    tmp_path: Path,
) -> None:
    """삭제 파일이 changed_files 에 있어도 fallback 강제 발동되면 안 됨.

    회귀 방지 (codex PR #26 review #6): 이전 `_changed_missing` 가
    `cf not in entries and cf not in filtered_out` 검사 → 삭제 파일은 disk 에 없으니
    entries 에도 없고, `git ls-files` 가 안 잡으니 filtered_out 에도 없음 → budget cut
    이 아닌데도 missing 으로 오판해 강제 fallback. 사용자에겐 삭제 파일 1개로 인해 모든
    리뷰가 diff-only 모드가 되는 회귀.

    이제는 `_changed_missing` 가 `cf in budget_excluded` 직접 검사 → 삭제 파일은 budget
    cut 이 아니라 정상 review 경로 진행.
    """
    github = FakeGitHub()
    pr = _sample_pr()  # changed_files=("a.py",) — 삭제됐다고 가정
    # 삭제 파일은 entries / filtered_out / budget_excluded 어디에도 없음
    # (collector 가 disk 에 없는 파일을 처리하지 않으므로). dump 는 정상적으로 다른
    # 파일들로 채워졌다고 시뮬레이션 — 예산 안에 들어감.
    dump = FileDump(
        entries=(FileEntry(path="other.py", content="x", size_bytes=1, is_changed=False),),
        total_chars=1,
        excluded=(),
        filtered_out=(),
        budget_excluded=(),  # 예산 cut 없음 — 코드베이스 전체가 예산 안에 들어감
        exceeded_budget=False,
        budget=TokenBudget(1000),
    )
    expected = ReviewResult(summary="normal-review", event=ReviewEvent.COMMENT)
    engine = FakeEngine(expected)
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(tmp_path),
        file_collector=FakeCollector(dump),
        engine=engine,
        finding_verifier=FakeFindingVerifier(),
        finding_deduper=FakeFindingDeduper(),
        resolution_checker=FakeFindingResolutionChecker(),
        max_input_tokens=1000,
    )

    use_case.execute(pr)

    assert engine.diff_calls == [], (
        "삭제 파일이 changed_files 에 있어도 budget cut 이 아니므로 fallback 진입 X"
    )
    assert github.posted_comments == [], "예산 초과 notice 게시 X"
    assert len(github.posted_reviews) == 1, "일반 review 경로 통과"
    assert github.posted_reviews[0][1] is expected


def test_use_case_does_not_fallback_when_only_filter_cut_changed_files(
    tmp_path: Path,
) -> None:
    """이미지/lock 같은 의도된 필터 제외 파일만 변경된 PR 은 강제 fallback 으로 빠지면 안 됨.

    회귀 방지 (gemini PR #26 review #3): 이전엔 dump 의 `excluded` 가 filter + budget
    cut 을 구분 없이 담고 `_changed_missing` 가 단순 `cf not in entries` 검사라, 이미지
    1개만 변경된 PR 도 `exceeded_budget=True` + `_changed_missing=True` 로 판정돼 강제
    diff fallback 으로 빠짐. 이제는 dump.filtered_out 가 분리 보고되어 use case 의
    `_changed_missing` 가 의도된 필터 제외를 missing 신호로 카운트하지 않음.

    이 테스트는 변경 파일이 모두 filtered_out 에 있고 budget_excluded 가 비었다는 상황
    → exceeded_budget=False → 일반 review 경로 (fallback 진입 자체 안 함).
    """
    github = FakeGitHub()
    pr = _sample_pr()  # changed_files=("a.py",)
    # 변경 파일이 필터로 제외됐다고 시뮬레이션 (FileDumpCollector 가 만드는 상태)
    dump = FileDump(
        entries=(),
        total_chars=0,
        excluded=("a.py",),
        filtered_out=("a.py",),  # 필터 제외만 — 예산 cut 아님
        budget_excluded=(),
        exceeded_budget=False,  # filter-only 는 budget 신호가 아님
        budget=TokenBudget(1000),
    )
    expected = ReviewResult(summary="normal-review", event=ReviewEvent.COMMENT)
    engine = FakeEngine(expected)
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(tmp_path),
        file_collector=FakeCollector(dump),
        engine=engine,
        finding_verifier=FakeFindingVerifier(),
        finding_deduper=FakeFindingDeduper(),
        resolution_checker=FakeFindingResolutionChecker(),
        max_input_tokens=1000,
    )

    use_case.execute(pr)

    assert engine.diff_calls == [], "필터 제외만이면 diff fallback 진입 X"
    assert github.posted_comments == [], "예산 초과 notice 게시 X"
    assert len(github.posted_reviews) == 1, "일반 review 경로 통과"
    assert github.posted_reviews[0][1] is expected


def test_use_case_size_check_uses_full_prompt_not_just_diff_text(
    tmp_path: Path,
) -> None:
    """예산 초과 검사는 build_diff_prompt 결과 전체 길이로 — diff_text 만 검사하면 회귀.

    회귀 방지 (codex PR #26 review #1): 이전엔 `len(diff_text) > max_chars` 만 검사해
    SYSTEM_RULES + DIFF_MODE_NOTICE + PR 메타 overhead 가 추가되면 실제 prompt 가
    예산 초과해 모델이 거부하는 경계가 남았다. 이제는 build_diff_prompt(pr, diff_text)
    결과 길이로 검사해 fallback 전 차단.

    이 테스트는 raw diff_text 는 작지만 (수십 chars) 예산을 SYSTEM_RULES 보다 작게
    잡아 prompt 전체로는 초과하는 경계 케이스. fallback 이 시도되면 안 되고 notice 가
    게시돼야.
    """
    github = FakeGitHub()
    pr = PullRequest(
        repo=RepoRef("o", "r"),
        number=42,
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
        # raw patch 는 소량 (~50 chars)
        file_patches=(("a.py", "@@ -1,1 +1,1 @@\n-x\n+y\n"),),
        addable_lines=(("a.py", frozenset({1})),),  # +y 는 RIGHT 1
    )
    # SYSTEM_RULES 만 ~5KB. token=500 (= 2000 chars) 면 SYSTEM_RULES 도 못 담음 →
    # diff_text 본문 길이 (50 chars 미만) 만 검사하던 이전 로직은 이 케이스를 통과시켜
    # engine 호출까지 갔던 회귀를 lock.
    dump = FileDump(
        entries=(),
        total_chars=0,
        excluded=("a.py",),
        budget_excluded=("a.py",),  # codex PR #26 review #6: budget cut 직접 명시
        exceeded_budget=True,
        budget=TokenBudget(500),
    )
    engine = FakeEngine(ReviewResult(summary="x", event=ReviewEvent.COMMENT))
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(tmp_path),
        file_collector=FakeCollector(dump),
        engine=engine,
        finding_verifier=FakeFindingVerifier(),
        finding_deduper=FakeFindingDeduper(),
        resolution_checker=FakeFindingResolutionChecker(),
        max_input_tokens=500,
    )

    use_case.execute(pr)

    assert engine.diff_calls == [], (
        "diff 본문은 작아도 SYSTEM_RULES + DIFF_MODE_NOTICE + 메타 합치면 예산 초과 → "
        "engine 호출 없이 notice"
    )
    assert github.posted_reviews == []
    assert len(github.posted_comments) == 1
    assert "예산 초과" in github.posted_comments[0][1]


def test_use_case_posts_notice_when_diff_itself_too_large(
    tmp_path: Path,
) -> None:
    """예산 초과 + diff 도 budget 초과 → notice (engine 호출 안 함, rate-limit 절감).

    회귀 방지: diff fallback 이 무조건 시도되면 거대 patch 가 들어오는 PR 에서 모델
    호출이 100% 실패. 게시 가치 없는 호출은 사전 차단해 GEMINI 호출 비용 + 시간 절감.
    """
    github = FakeGitHub()
    huge_patch = "@@ -1,1 +1,1 @@\n" + ("+x\n" * 5000)  # 약 15000 chars
    pr = PullRequest(
        repo=RepoRef("o", "r"),
        number=42,
        title="대형 PR",
        body="",
        head_sha="abc",
        head_ref="feat",
        base_sha="def",
        base_ref="main",
        clone_url="https://example/x.git",
        changed_files=("big.py",),
        installation_id=7,
        is_draft=False,
        file_patches=(("big.py", huge_patch),),
        # huge_patch: `@@ -1,1 +1,1 @@\n` + ("+x\n" * 5000) → RIGHT 1..5000
        addable_lines=(("big.py", frozenset(range(1, 5001))),),
    )
    dump = FileDump(
        entries=(),
        total_chars=0,
        excluded=("big.py",),
        budget_excluded=("big.py",),  # codex PR #26 review #6: budget cut 직접 명시
        exceeded_budget=True,
        budget=TokenBudget(100),  # 100 tokens × 4 chars = 400 char budget — diff 훨씬 큼
    )
    engine = FakeEngine(ReviewResult(summary="x", event=ReviewEvent.COMMENT))
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(tmp_path),
        file_collector=FakeCollector(dump),
        engine=engine,
        finding_verifier=FakeFindingVerifier(),
        finding_deduper=FakeFindingDeduper(),
        resolution_checker=FakeFindingResolutionChecker(),
        max_input_tokens=100,
    )

    use_case.execute(pr)

    assert engine.diff_calls == [], "diff 가 너무 커서 engine 호출 안 함"
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
        finding_verifier=FakeFindingVerifier(),
        finding_deduper=FakeFindingDeduper(),
        resolution_checker=FakeFindingResolutionChecker(),
        max_input_tokens=1000,
    )

    use_case.execute(pr)

    assert github.posted_comments == []
    assert len(github.posted_reviews) == 1
    assert github.posted_reviews[0][1] is expected


def test_use_case_chains_verify_then_dedupe_then_post_then_resolution_check(
    tmp_path: Path,
) -> None:
    """오케스트레이션 순서 lock: engine → verifier → deduper → post_review → resolution_check.

    회귀 방지:
    - dedup 결과가 post_review 에 도달해야 한다 (Layer D 강등 → 게시 반영).
    - deduper 가 verifier 출력을 받아야 (순서 뒤집히면 강등 전 원본 등급이 dedup
      signature 에 쓰여 false positive).
    - resolution_check 는 post_review **이후** 호출 (Layer E — 게시 끝난 코멘트 history
      를 바탕으로 follow-up. 게시 전에 호출되면 이번 push 의 새 코멘트도 포함돼 의미
      없음).
    """
    github = FakeGitHub()
    pr = _sample_pr()

    dump = FileDump(
        entries=(FileEntry(path="a.py", content="x=1", size_bytes=3, is_changed=True),),
        total_chars=3,
        exceeded_budget=False,
    )
    engine_result = ReviewResult(summary="from-engine", event=ReviewEvent.COMMENT)
    verified_result = ReviewResult(summary="from-verifier", event=ReviewEvent.COMMENT)
    deduped_result = ReviewResult(summary="from-deduper", event=ReviewEvent.COMMENT)

    call_order: list[str] = []

    class _OrderObservingVerifier:
        def __init__(self) -> None:
            self.received: ReviewResult | None = None

        def verify(self, result: ReviewResult, repo_root: Path) -> ReviewResult:
            self.received = result
            call_order.append("verify")
            return verified_result

    class _OrderObservingDeduper:
        def __init__(self) -> None:
            self.received: ReviewResult | None = None

        def dedupe(self, result: ReviewResult, pr: PullRequest) -> ReviewResult:
            self.received = result
            call_order.append("dedupe")
            return deduped_result

    class _OrderObservingResolutionChecker:
        def check_resolutions(self, pr: PullRequest, repo_root: Path) -> None:
            call_order.append("resolution_check")

    class _OrderObservingGitHub(FakeGitHub):
        def post_review(self, pr: PullRequest, result: ReviewResult) -> None:
            call_order.append("post_review")
            super().post_review(pr, result)

    verifier = _OrderObservingVerifier()
    deduper = _OrderObservingDeduper()
    resolver = _OrderObservingResolutionChecker()
    github = _OrderObservingGitHub()
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(tmp_path),
        file_collector=FakeCollector(dump),
        engine=FakeEngine(engine_result),
        finding_verifier=verifier,
        finding_deduper=deduper,
        resolution_checker=resolver,
        max_input_tokens=1000,
    )

    use_case.execute(pr)

    assert verifier.received is engine_result, "verifier 는 engine 출력을 받아야"
    assert deduper.received is verified_result, "deduper 는 verifier 출력을 받아야 (순서 lock)"
    assert call_order == ["verify", "dedupe", "post_review", "resolution_check"], (
        f"순서가 어긋나면 안 됨. 실제: {call_order}"
    )
    assert len(github.posted_reviews) == 1
    assert github.posted_reviews[0][1] is deduped_result, "게시되는 건 deduper 출력"


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
        finding_verifier=FakeFindingVerifier(),
        finding_deduper=FakeFindingDeduper(),
        resolution_checker=FakeFindingResolutionChecker(),
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
        finding_verifier=FakeFindingVerifier(),
        finding_deduper=FakeFindingDeduper(),
        resolution_checker=FakeFindingResolutionChecker(),
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
