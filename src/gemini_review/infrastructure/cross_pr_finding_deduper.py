"""Cross-PR finding dedup — Layer D 후처리 강등.

사용자 신고 사례 (2026-04, swift-man/MaterialDesignColor PR #7) 의 phantom whitespace
finding 이 4 회 연속 push 에 걸쳐 동일 [Major] 코멘트로 재발행돼 alert fatigue 를
유발한 회귀에 대한 방어:

1. 본 봇이 게시한 PR 의 기존 인라인 리뷰 코멘트를 GitHub API 로 조회.
2. 새 [Critical]/[Major] finding 의 `(path, line, severity-stripped body)` 가 그 기존
   코멘트 하나라도 일치하면 [Suggestion] 으로 강등 — "이전 push 에서 동일 지적이 이미
   게시됐고 메인테이너가 무시한 것으로 판단됨" 이라는 history grounding.
3. 강등 후 blocking 분포가 바뀌면 `_normalize_event` 가 event 를 재정합 (Layer B 와
   동일 패턴).

Layer B (`SourceGroundedFindingVerifier`) 는 단일 finding 의 출처 grounding 이고 이
Layer D 는 PR 단위 history grounding — 책임이 다르다. 두 레이어 모두 통과한 finding
만이 [Critical]/[Major] 등급을 그대로 유지하고 게시된다.
"""
import dataclasses
import logging
import re

from gemini_review.domain import (
    Finding,
    PostedReviewComment,
    PullRequest,
    ReviewResult,
)
from gemini_review.infrastructure.gemini_parser import _normalize_event
from gemini_review.infrastructure.source_grounded_finding_verifier import (
    _BLOCKING_SEVERITIES,
    _SEVERITY_PREFIX_HEAD,
)
from gemini_review.interfaces import GitHubClient

logger = logging.getLogger(__name__)

# Layer B/D 가 강등한 본문에 붙는 자동 강등 마커 prefix 를 지우는 정규식.
#
# 형태: `(자동 강등: <reason>, 원래 [<severity>]) <원본 본문>`
# 예시:
#   - Layer B (phantom-quote): `(자동 강등: 인용 텍스트 ... 의 실제 라인에 없음 — phantom
#     quote 환각 가능성, 원래 [Major]) <원본>`
#   - Layer B (file unverifiable): `(자동 강등: README.md:120 의 실제 라인을 검증할 수
#     없음 [missing] — 모델이 본 파일이 아닐 가능성, 원래 [Critical]) <원본>`
#   - Layer D (history dedup): `(자동 강등: 이전 push 에서 동일 지적이 이미 게시됨 —
#     메인테이너가 무시한 것으로 판단됨, 원래 [Major]) <원본>`
#
# 공통 anchor: `^(자동 강등:` 시작 + `, 원래 [SEVERITY]) ` 종료. `.*?` 는 non-greedy.
# `re.DOTALL` 로 멀티라인 강등 사유도 처리 가능.
#
# 회귀 방지 (codex PR #25 review #1): 이 정규화가 없으면 2 회차에 강등된 코멘트가 3
# 회차의 새 [Major] finding 과 시그니처 비교 시 match 실패 — Layer D 가 2 회차에서만
# 작동하고 그 이후엔 무력화돼 phantom finding 이 다시 [Major] 로 게시됨.
_AUTO_DEMOTE_PREFIX = re.compile(
    r"^\(자동 강등:.*?, 원래 \[(?:Critical|Major|Minor|Suggestion)\]\)\s*",
    re.DOTALL,
)


class CrossPrFindingDeduper:
    """본 봇이 같은 PR 에 이미 게시한 finding 과 중복되는 차단급 finding 을 강등."""

    def __init__(self, github: GitHubClient) -> None:
        self._github = github

    def dedupe(self, result: ReviewResult, pr: PullRequest) -> ReviewResult:
        """이전 push 와 중복되는 [Critical]/[Major] finding 을 [Suggestion] 으로 강등.

        ### 강등 조건 (모두 만족)

        1. finding body 가 [Critical] 또는 [Major] 시작
        2. 같은 PR 의 본 봇 기존 인라인 코멘트 중 `(path, line, severity-stripped body)`
           가 정확히 일치하는 것이 있음

        Body 매칭은 **severity prefix 를 떼고 strip 한 본문의 정확 일치**.
        - 모델이 [Critical] → [Major] 로 등급만 바꿔 다시 올린 경우도 dedup 됨 (의도).
        - 단어 한 글자만 바꿔 우회하면 dedup 안 됨 (의도된 보수적 선택). 퍼지 매칭은
          서로 다른 정당 finding 을 묶어 버릴 위험이 더 커서 v1 은 정확 매칭만.

        ### 단락 (short-circuit) — API 호출 비용 절감

        result.findings 에 [Critical]/[Major] 가 0 건이면 dedup 비교 자체가 무의미하므로
        GitHub API 호출 없이 바로 result 반환. 정상 PR (블로킹 finding 없음) 의 매 리뷰가
        의미 없는 round-trip 을 발생시키지 않도록.

        ### Graceful degrade — Layer D 는 방어 레이어이지 차단 레이어가 아님

        `list_self_review_comments` 가 실패해도 (네트워크/Auth/rate-limit) 리뷰 자체를
        막지 않는다. WARN 로그만 남기고 원본 result 반환 — 그러면 dedup 이 잠시 작동하지
        않을 뿐 정상 게시 흐름은 유지. dedup 부재의 비용 (alert fatigue 일시 재현) 이
        리뷰 게시 실패 비용보다 훨씬 작다.
        """
        if not _has_blocking(result.findings):
            return result
        try:
            existing = self._github.list_self_review_comments(pr)
        except Exception as exc:  # noqa: BLE001 — graceful degrade, 어떤 실패도 리뷰를 막지 않음
            logger.warning(
                "list_self_review_comments failed for %s#%d (%s); skipping dedup, "
                "review will post without history-grounding",
                pr.repo.full_name,
                pr.number,
                exc,
            )
            return result
        if not existing:
            return result
        signatures = _build_signatures(existing)
        new_findings = tuple(
            self._maybe_demote(f, signatures, pr) for f in result.findings
        )
        new_event = _normalize_event(result.event, new_findings)
        return dataclasses.replace(result, findings=new_findings, event=new_event)

    def _maybe_demote(
        self,
        f: Finding,
        signatures: set[tuple[str, int, str]],
        pr: PullRequest,
    ) -> Finding:
        head = _SEVERITY_PREFIX_HEAD.match(f.body)
        if head is None:
            return f
        severity, rest = head.group(1), head.group(2)
        if severity not in _BLOCKING_SEVERITIES:
            return f
        key = (f.path, f.line, _normalize_body_for_match(rest))
        if key not in signatures:
            return f
        logger.warning(
            "demoting severity %s -> Suggestion (cross-PR dedup): %s:%d already posted "
            "in earlier push of %s#%d; assuming maintainer ignored.",
            severity,
            f.path,
            f.line,
            pr.repo.full_name,
            pr.number,
        )
        return Finding(
            path=f.path,
            line=f.line,
            body=(
                f"[Suggestion] (자동 강등: 이전 push 에서 동일 지적이 이미 게시됨 — "
                f"메인테이너가 무시한 것으로 판단됨, 원래 [{severity}]) {rest}"
            ),
        )


def _has_blocking(findings: tuple[Finding, ...]) -> bool:
    """findings 에 [Critical]/[Major] 가 하나라도 있으면 True — short-circuit gate."""
    for f in findings:
        head = _SEVERITY_PREFIX_HEAD.match(f.body)
        if head is None:
            continue
        if head.group(1) in _BLOCKING_SEVERITIES:
            return True
    return False


def _build_signatures(
    existing: tuple[PostedReviewComment, ...],
) -> set[tuple[str, int, str]]:
    """기존 코멘트들에서 dedup key 셋 구성.

    Severity prefix 를 떼고 strip 한 본문이 시그니처의 일부 — 이전 push 에서 이미
    [Suggestion] 으로 강등돼 게시된 코멘트도 새 [Critical]/[Major] 와 매칭되도록.
    같은 표현이 어떤 등급으로 다시 올라와도 dedup 발동.
    """
    sigs: set[tuple[str, int, str]] = set()
    for c in existing:
        head = _SEVERITY_PREFIX_HEAD.match(c.body)
        rest = head.group(2) if head is not None else c.body
        sigs.add((c.path, c.line, _normalize_body_for_match(rest)))
    return sigs


def _normalize_body_for_match(rest: str) -> str:
    """매칭용 본문 정규화 — 자동 강등 prefix 제거 + 양끝 공백/줄바꿈 strip.

    `rest` 는 이미 severity prefix 가 떨어진 본문. 추가로 Layer B/D 가 강등할 때 붙이는
    `(자동 강등: ..., 원래 [SEVERITY]) ` prefix 를 제거해야 한다 (codex PR #25 review #1):

    - 1 회차 push: 모델 finding `[Major] phantom 본문` 게시
    - 2 회차 push: dedup 발동 → `[Suggestion] (자동 강등: 이전 push ..., 원래 [Major])
                   phantom 본문` 게시
    - 3 회차 push: 모델 finding `[Major] phantom 본문` (또 같은 단언). 이전 코멘트 (2
                   회차의 강등본) 와 시그니처 비교 시 prefix 가 남아 있으면 match 실패
                   → dedup 무력화 → 4 회차 5 회차 계속 게시되는 alert fatigue 재현.

    prefix 도 제거하면 모든 회차의 시그니처가 `phantom 본문` 으로 동일 → 안정적 dedup.

    내부 표현 (단어/구두점) 은 그대로 보존 — 더 적극적 정규화 (whitespace collapse,
    마크다운 strip 등) 는 서로 다른 정당 finding 을 같은 시그니처로 묶을 위험이 있어
    v1 은 보수적으로 prefix strip + outer trim 만 한다.
    """
    stripped = _AUTO_DEMOTE_PREFIX.sub("", rest, count=1)
    return stripped.strip()
