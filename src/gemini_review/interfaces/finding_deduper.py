from typing import Protocol

from gemini_review.domain import PullRequest, ReviewResult


class FindingDeduper(Protocol):
    """후처리 검증 (`FindingVerifier`) 이후 cross-push 중복 finding 을 강등하는 Layer D.

    같은 PR 의 이전 push 에서 본 봇이 이미 게시한 [Critical]/[Major] finding 이 새
    리뷰에 그대로 다시 등장하면, 메인테이너가 이전 push 에서 무시한 신호로 보고
    [Suggestion] 으로 강등한다. 사용자 신고 사례 (2026-04, MaterialDesignColor PR #7)
    의 phantom whitespace finding 이 4 회 연속 push 에 걸쳐 동일 코멘트로 반복 게시되며
    alert fatigue 를 유발한 회귀에 대한 방어다.

    구현체 (`CrossPrFindingDeduper`) 는 GitHub API 로 본 봇의 기존 코멘트를 조회해
    `(path, line, severity-stripped body)` 시그니처로 매칭한다. 다른 dedup 전략 (예:
    퍼지 매칭, 모델 임베딩 거리) 으로 교체 가능하도록 Protocol 로 분리.

    Layer B (FindingVerifier) 와 책임이 분리돼 있다 — Verifier 는 단일 finding 의 출처
    grounding, Deduper 는 PR 단위 history grounding. 두 layer 가 모두 통과한 finding
    만이 [Critical]/[Major] 등급을 그대로 유지하고 게시된다.
    """

    def dedupe(self, result: ReviewResult, pr: PullRequest) -> ReviewResult:
        """이전 push 에 게시된 finding 과 중복되는 [Critical]/[Major] 를 강등해 반환.

        구현체는 이전 코멘트 조회에 실패해도 (네트워크/API 오류) 리뷰 자체를 막지 않고
        원본 `result` 를 반환해야 한다 — Layer D 는 방어 레이어이지 차단 레이어가 아님.

        강등으로 blocking 분포가 바뀌면 event 도 같이 재정합 (Layer B 와 동일 패턴).
        """
        ...
