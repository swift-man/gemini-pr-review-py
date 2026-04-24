"""CrossPrFindingDeduper 단위 테스트.

핵심: 본 봇이 같은 PR 의 이전 push 에서 게시한 [Critical]/[Major] finding 이 새 리뷰에
같은 (path, line, severity-stripped body) 로 다시 등장하면 [Suggestion] 으로 강등.

회귀 방지 (사용자 신고, 2026-04, swift-man/MaterialDesignColor PR #7): phantom whitespace
finding 이 4 회 연속 push 에 걸쳐 동일 [Major] 코멘트로 재발행돼 alert fatigue 유발.
Layer D 가 2 회차부터 [Suggestion] 으로 강등해 noise 차단.
"""
import logging

import pytest

from gemini_review.domain import (
    Finding,
    PostedReviewComment,
    PullRequest,
    RepoRef,
    ReviewEvent,
    ReviewResult,
)
from gemini_review.infrastructure.cross_pr_finding_deduper import (
    CrossPrFindingDeduper,
)


def _pr() -> PullRequest:
    return PullRequest(
        repo=RepoRef("o", "r"),
        number=42,
        title="t",
        body="",
        head_sha="abc",
        head_ref="feat",
        base_sha="def",
        base_ref="main",
        clone_url="https://example/r.git",
        changed_files=("a.py",),
        installation_id=7,
        is_draft=False,
    )


def _result(*findings: Finding, event: ReviewEvent = ReviewEvent.REQUEST_CHANGES) -> ReviewResult:
    return ReviewResult(summary="x", event=event, findings=findings)


class _FakeGitHub:
    """GitHubClient 프로토콜의 dedup 관련 메서드만 fake. 다른 메서드는 호출되지 않아야."""

    def __init__(
        self,
        existing: tuple[PostedReviewComment, ...] = (),
        raise_on_list: Exception | None = None,
    ) -> None:
        self._existing = existing
        self._raise = raise_on_list
        self.list_call_count = 0

    def list_self_review_comments(self, pr: PullRequest) -> tuple[PostedReviewComment, ...]:
        self.list_call_count += 1
        if self._raise is not None:
            raise self._raise
        return self._existing

    # 다른 메서드는 dedup 흐름에서 호출되면 안 됨 — 잘못 wiring 회귀 방지
    def fetch_pull_request(self, *_a: object, **_k: object) -> object:
        raise AssertionError("fetch_pull_request should not be called by deduper")

    def post_review(self, *_a: object, **_k: object) -> None:
        raise AssertionError("post_review should not be called by deduper")

    def post_comment(self, *_a: object, **_k: object) -> None:
        raise AssertionError("post_comment should not be called by deduper")

    def get_installation_token(self, *_a: object, **_k: object) -> str:
        raise AssertionError("get_installation_token should not be called by deduper")


# --- 강등 발동 ---------------------------------------------------------------


def test_dedupe_demotes_when_same_path_line_body_already_posted() -> None:
    """같은 (path, line, severity-stripped body) 가 history 에 있으면 [Major] → [Suggestion].

    회귀 방지: 4 회 연속 push 동일 phantom 코멘트 시나리오. 2 회차부터 강등.
    """
    existing = (
        PostedReviewComment(
            path="README.md",
            line=120,
            body="[Major] 패키지명 앞에 공백(`\" @scope\"`)이 있습니다.",
        ),
    )
    new_finding = Finding(
        path="README.md",
        line=120,
        body="[Major] 패키지명 앞에 공백(`\" @scope\"`)이 있습니다.",
    )

    out = CrossPrFindingDeduper(_FakeGitHub(existing)).dedupe(_result(new_finding), _pr())

    assert out.findings[0].body.startswith("[Suggestion]"), "이전 push 와 중복 → 강등"
    assert "이전 push 에서 동일 지적이 이미 게시됨" in out.findings[0].body
    assert "원래 [Major]" in out.findings[0].body, "원래 등급 보존 (silent rewrite 방지)"


def test_dedupe_demotes_critical_against_previously_posted_critical() -> None:
    """[Critical] 도 dedup 발동 — phantom Critical 이 반복되는 alert fatigue 차단."""
    existing = (
        PostedReviewComment(
            path=".github/workflows/ci.yml",
            line=29,
            body="[Critical] CI 즉시 실패 단언 (잘못된 grounding).",
        ),
    )
    new_finding = Finding(
        path=".github/workflows/ci.yml",
        line=29,
        body="[Critical] CI 즉시 실패 단언 (잘못된 grounding).",
    )

    out = CrossPrFindingDeduper(_FakeGitHub(existing)).dedupe(_result(new_finding), _pr())

    assert out.findings[0].body.startswith("[Suggestion]")
    assert "원래 [Critical]" in out.findings[0].body


def test_dedupe_matches_after_severity_prefix_strip() -> None:
    """등급만 [Critical] → [Major] 로 바꿔 다시 올린 경우도 dedup 발동.

    의도: 모델이 같은 본문을 등급만 흔들어 alert fatigue 우회하는 것을 막는다. 시그니처는
    severity prefix 떼고 strip 한 본문 — 등급은 시그니처에 포함 안 됨.
    """
    existing = (
        PostedReviewComment(
            path="a.py",
            line=10,
            body="[Critical] 동일 본문 — 등급만 변경.",
        ),
    )
    new_finding = Finding(
        path="a.py",
        line=10,
        body="[Major] 동일 본문 — 등급만 변경.",
    )

    out = CrossPrFindingDeduper(_FakeGitHub(existing)).dedupe(_result(new_finding), _pr())

    assert out.findings[0].body.startswith("[Suggestion]"), "등급만 다른 동일 본문 → dedup"


def test_dedupe_event_renormalized_after_demotion() -> None:
    """블로킹 finding 이 모두 dedup 강등되면 REQUEST_CHANGES → COMMENT 로 재정합."""
    existing = (
        PostedReviewComment(
            path="a.py",
            line=10,
            body="[Major] 같은 지적 반복.",
        ),
    )
    new_finding = Finding(path="a.py", line=10, body="[Major] 같은 지적 반복.")

    out = CrossPrFindingDeduper(_FakeGitHub(existing)).dedupe(
        _result(new_finding, event=ReviewEvent.REQUEST_CHANGES), _pr()
    )

    assert out.event == ReviewEvent.COMMENT, "blocking 0 → event 약화"


# --- 강등 안 함 (false positive 방지) ---------------------------------------


def test_dedupe_keeps_when_different_line() -> None:
    """같은 path/body 이지만 다른 line 이면 강등 안 함.

    서로 다른 두 줄에서 같은 패턴이 정당하게 발생할 수 있다 (예: 두 함수 모두 같은
    유형의 미처리 케이스).
    """
    existing = (
        PostedReviewComment(path="a.py", line=10, body="[Major] 같은 패턴 다른 위치."),
    )
    new_finding = Finding(path="a.py", line=20, body="[Major] 같은 패턴 다른 위치.")

    out = CrossPrFindingDeduper(_FakeGitHub(existing)).dedupe(_result(new_finding), _pr())

    assert out.findings[0].body.startswith("[Major]"), "다른 line → dedup 발동 X"


def test_dedupe_keeps_when_different_body() -> None:
    """같은 path/line 이라도 본문이 다르면 강등 안 함 — 정당 finding 보존."""
    existing = (
        PostedReviewComment(path="a.py", line=10, body="[Major] 첫 push 의 지적."),
    )
    new_finding = Finding(path="a.py", line=10, body="[Major] 다른 내용의 새 지적.")

    out = CrossPrFindingDeduper(_FakeGitHub(existing)).dedupe(_result(new_finding), _pr())

    assert out.findings[0].body.startswith("[Major]"), "본문 다름 → dedup 발동 X"


def test_dedupe_keeps_when_different_path() -> None:
    """같은 line/body 이라도 다른 파일이면 강등 안 함."""
    existing = (
        PostedReviewComment(path="a.py", line=10, body="[Major] 같은 본문 다른 파일."),
    )
    new_finding = Finding(path="b.py", line=10, body="[Major] 같은 본문 다른 파일.")

    out = CrossPrFindingDeduper(_FakeGitHub(existing)).dedupe(_result(new_finding), _pr())

    assert out.findings[0].body.startswith("[Major]"), "다른 path → dedup 발동 X"


def test_dedupe_skips_minor_and_suggestion() -> None:
    """비-블로킹 finding ([Minor]/[Suggestion]) 은 dedup 발동 X — 이미 약한 신호.

    의도: dedup 의 가치는 차단 신호 약화. 이미 [Suggestion] 인 finding 을 다시 [Suggestion]
    으로 강등하는 것은 의미 없음. 또한 "메인테이너가 무시했다" 라는 framing 도 차단급에만
    적합 — 권고 finding 은 무시되는 게 정상이라서.
    """
    existing = (
        PostedReviewComment(path="a.py", line=10, body="[Minor] 권고."),
        PostedReviewComment(path="a.py", line=20, body="[Suggestion] 제안."),
    )
    new_findings = (
        Finding(path="a.py", line=10, body="[Minor] 권고."),
        Finding(path="a.py", line=20, body="[Suggestion] 제안."),
    )

    out = CrossPrFindingDeduper(_FakeGitHub(existing)).dedupe(
        _result(*new_findings, event=ReviewEvent.COMMENT), _pr()
    )

    assert out.findings[0].body.startswith("[Minor]"), "[Minor] 보존"
    assert out.findings[1].body.startswith("[Suggestion]"), "[Suggestion] 보존"


# --- Short-circuit / graceful degrade ---------------------------------------


def test_dedupe_skips_api_call_when_no_blocking_findings() -> None:
    """블로킹 finding 이 0 건이면 API 호출 자체를 건너뜀 — 정상 PR 의 round-trip 절감.

    회귀 방지: 매 리뷰마다 무조건 list_self_review_comments 를 호출하면 정상 PR (블로킹
    없음) 도 GitHub API rate limit 을 소비. 의미 없는 호출이므로 short-circuit.
    """
    fake = _FakeGitHub(existing=())  # 호출되면 안 됨
    new_findings = (
        Finding(path="a.py", line=10, body="[Minor] 권고만 있음."),
        Finding(path="a.py", line=20, body="[Suggestion] 제안만."),
    )

    out = CrossPrFindingDeduper(fake).dedupe(
        _result(*new_findings, event=ReviewEvent.COMMENT), _pr()
    )

    assert fake.list_call_count == 0, "블로킹 0건 → API 호출 안 함"
    assert out.findings == new_findings, "변경 없이 그대로 반환"


def test_dedupe_skips_when_history_empty() -> None:
    """API 는 호출하되 history 가 비었으면 강등 없이 그대로 반환."""
    fake = _FakeGitHub(existing=())
    new_finding = Finding(path="a.py", line=10, body="[Major] 첫 push 의 지적.")

    out = CrossPrFindingDeduper(fake).dedupe(_result(new_finding), _pr())

    assert fake.list_call_count == 1, "블로킹 있음 → API 호출 1회"
    assert out.findings[0].body.startswith("[Major]"), "history 비어 있음 → 보존"


def test_dedupe_graceful_degrade_on_api_error(caplog: pytest.LogCaptureFixture) -> None:
    """API 가 실패해도 리뷰를 막지 않고 원본 result 반환 + WARN 로그.

    회귀 방지: dedup 은 방어 레이어이지 차단 레이어가 아님. GitHub API 가 일시적 장애
    (네트워크/auth/rate-limit) 를 겪어도 리뷰 게시 자체는 진행돼야 함. dedup 부재의 비용
    (alert fatigue 일시 재현) < 리뷰 게시 실패 비용.
    """
    fake = _FakeGitHub(raise_on_list=RuntimeError("rate limit exceeded"))
    new_finding = Finding(path="a.py", line=10, body="[Major] 정당한 지적.")

    with caplog.at_level(logging.WARNING):
        out = CrossPrFindingDeduper(fake).dedupe(_result(new_finding), _pr())

    assert out.findings[0].body.startswith("[Major]"), "API 실패 → 원본 보존"
    assert any(
        "list_self_review_comments failed" in r.getMessage() for r in caplog.records
    ), "운영 진단을 위해 WARN 1건 이상"


# --- 시그니처 정규화 details -------------------------------------------------


def test_dedupe_matches_against_previously_auto_demoted_history_layer_d() -> None:
    """3 회차+ 회귀 방지: Layer D 가 이전에 강등한 [Suggestion] 본문도 dedup 시그니처에서 매칭.

    회귀 방지 (codex PR #25 review #1): _normalize_body_for_match 가 severity prefix 만
    떼고 자동 강등 prefix 를 그대로 두면 다음 시나리오가 깨졌다.

    1 회차: 모델 `[Major] phantom 본문` → history 기록.
    2 회차: 모델 `[Major] phantom 본문` 다시 → dedup 발동 → `[Suggestion]
            (자동 강등: 이전 push 에서 동일 지적이 이미 게시됨 ..., 원래 [Major])
            phantom 본문` 게시 → 이게 history 에 남음.
    3 회차: 모델 `[Major] phantom 본문` 또 다시 → 이전 history 코멘트의 시그니처가
            `(자동 강등: ..., 원래 [Major]) phantom 본문` (prefix 안 떨어짐) → 새
            finding 의 시그니처 `phantom 본문` 과 다름 → match 실패 → dedup 무력화.

    이제는 _AUTO_DEMOTE_PREFIX 가 prefix 를 떼므로 시그니처가 `phantom 본문` 으로 동일,
    3 회차+ 도 안정 강등.
    """
    existing = (
        # 2 회차 dedup 결과 게시된 본문 — 자동 강등 prefix 포함
        PostedReviewComment(
            path="README.md",
            line=120,
            body=(
                "[Suggestion] (자동 강등: 이전 push 에서 동일 지적이 이미 게시됨 — "
                "메인테이너가 무시한 것으로 판단됨, 원래 [Major]) phantom 공백 본문."
            ),
        ),
    )
    # 3 회차 push 의 새 finding — 모델은 또 원본 형태로 단언
    new_finding = Finding(
        path="README.md", line=120, body="[Major] phantom 공백 본문."
    )

    out = CrossPrFindingDeduper(_FakeGitHub(existing)).dedupe(_result(new_finding), _pr())

    assert out.findings[0].body.startswith("[Suggestion]"), (
        "3 회차 dedup: history 에 자동 강등본만 있어도 새 [Major] 가 강등돼야"
    )
    assert "원래 [Major]" in out.findings[0].body, (
        "3 회차 강등도 원래 등급은 [Major] (history 의 '원래 [Major]' 가 아니라 새 finding 의 등급)"
    )


def test_dedupe_matches_against_previously_layer_b_demoted_history() -> None:
    """Layer B 강등 본문도 dedup 매칭 — 출처 검증으로 강등된 phantom 도 history grounding 통과.

    시나리오: Layer B (verifier) 가 1 회차에 phantom-quote 강등 → 2 회차 모델이 같은
    단언 다시 → Layer D 가 history 의 Layer-B-강등본과 매칭해 또 강등. 두 layer 의
    강등 prefix 가 모두 정규화에서 떨어져야 함.
    """
    existing = (
        PostedReviewComment(
            path="README.md",
            line=120,
            body=(
                "[Suggestion] (자동 강등: 인용 텍스트 `\" @scope\"` 등이 README.md:120 의 "
                "실제 라인에 없음 — phantom quote 환각 가능성, 원래 [Major]) "
                "패키지명 앞에 공백."
            ),
        ),
    )
    new_finding = Finding(
        path="README.md", line=120, body="[Major] 패키지명 앞에 공백."
    )

    out = CrossPrFindingDeduper(_FakeGitHub(existing)).dedupe(_result(new_finding), _pr())

    assert out.findings[0].body.startswith("[Suggestion]"), (
        "Layer B 강등본 history 와 새 [Major] 매칭돼 dedup 강등돼야"
    )


def test_dedupe_matches_when_only_whitespace_padding_differs() -> None:
    """양끝 공백/줄바꿈은 strip 으로 정규화 — 사소한 형식 차이로 dedup 우회 못 하도록."""
    existing = (
        PostedReviewComment(
            path="a.py", line=10, body="[Major]   동일 본문 (앞뒤 공백 차이).  "
        ),
    )
    new_finding = Finding(
        path="a.py", line=10, body="[Major] 동일 본문 (앞뒤 공백 차이)."
    )

    out = CrossPrFindingDeduper(_FakeGitHub(existing)).dedupe(_result(new_finding), _pr())

    assert out.findings[0].body.startswith("[Suggestion]"), "양끝 공백만 다름 → dedup 발동"


def test_dedupe_keeps_when_internal_word_differs() -> None:
    """단어 한 글자만 달라도 dedup 안 함 — 내부 표현은 보존, 정확 매칭만.

    회귀 방지: v1 정책상 정확 매칭. 퍼지 매칭 (Jaccard 등) 은 서로 다른 정당 finding 을
    묶을 위험이 더 커서 채택 안 함. 모델이 한 글자 바꿔 우회하는 부작용은 받아들임 —
    그 비용은 phantom finding 첫 게시와 거의 같음.
    """
    existing = (
        PostedReviewComment(path="a.py", line=10, body="[Major] 변수명 typo: usrname"),
    )
    new_finding = Finding(path="a.py", line=10, body="[Major] 변수명 typo: usename")  # 한 글자 다름

    out = CrossPrFindingDeduper(_FakeGitHub(existing)).dedupe(_result(new_finding), _pr())

    assert out.findings[0].body.startswith("[Major]"), "내부 한 글자 다름 → 보존 (정확 매칭)"
