from typing import Protocol

from gemini_review.domain import FileDump, PullRequest, ReviewResult


class ReviewEngine(Protocol):
    """프롬프트를 LLM 에 태워 구조화된 리뷰 결과로 되돌려주는 추상화.

    기본 구현은 `GeminiCliEngine` (Gemini CLI + Google OAuth) 이지만, 동일한
    Protocol 을 만족하면 다른 모델(로컬 MLX, Codex 등) 로 교체 가능합니다 (OCP).
    """

    def review(self, pr: PullRequest, dump: FileDump) -> ReviewResult:
        """전체 코드베이스 덤프를 입력으로 한국어 리뷰 결과(JSON 스키마) 를 반환합니다."""
        ...
