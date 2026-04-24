from pathlib import Path
from typing import Protocol

from gemini_review.domain import ReviewResult


class FindingVerifier(Protocol):
    """파서 통과 후 ReviewResult 의 finding 들을 실제 소스에 대고 한 번 더 검증.

    파서 단계의 패턴 강등 (`_HALLUCINATION_PATTERNS`) 은 알려진 환각 표현만 잡지만,
    이 검증은 모델이 본문에 인용한 텍스트가 실제 `path:line` 에 존재하는지 디스크
    레벨에서 확인. 새 환각 표현이 등장해도 "원본에 X 있다" 단언이 거짓이면 잡힘.

    구현체 (`SourceGroundedFindingVerifier`) 는 repo 체크아웃 디렉터리에서 라인을 읽어
    quote 검증을 수행하지만, 다른 검증 전략 (예: AST 기반, LSP 기반) 으로 교체 가능.
    """

    def verify(self, result: ReviewResult, repo_root: Path) -> ReviewResult:
        """검증 결과로 일부 finding 의 등급이 강등될 수 있다 — 새 ReviewResult 반환.

        구현체는 phantom assertion 을 가진 [Critical]/[Major] finding 을 [Suggestion]
        으로 강등하고, 강등으로 blocking 분포가 바뀌면 event 도 같이 재정합한다.
        """
        ...
