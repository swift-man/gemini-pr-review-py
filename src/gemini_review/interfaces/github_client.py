from typing import Protocol

from gemini_review.domain import PullRequest, RepoRef, ReviewResult


class GitHubClient(Protocol):
    """GitHub REST API 를 다루는 추상화.

    GitHub App 설치 토큰 기반 인증이 기본 전제이며, 애플리케이션 계층은
    구체 구현(`GitHubAppClient`) 이 아니라 이 Protocol 에 의존합니다 (DIP).
    """

    def fetch_pull_request(
        self, repo: RepoRef, number: int, installation_id: int
    ) -> PullRequest:
        """PR 메타데이터와 변경 파일 목록을 조회해 도메인 객체로 반환합니다."""
        ...

    def post_review(
        self,
        pr: PullRequest,
        result: ReviewResult,
    ) -> None:
        """본문(좋은 점 / 개선할 점) + 라인 고정 인라인 코멘트를 PR 에 한 번에 게시합니다."""
        ...

    def post_comment(
        self,
        pr: PullRequest,
        body: str,
    ) -> None:
        """PR 에 단순 이슈 코멘트를 게시합니다. 예산 초과 안내 등에 사용합니다."""
        ...

    def get_installation_token(self, installation_id: int) -> str:
        """App JWT 로 설치 토큰을 발급/캐싱해 반환합니다."""
        ...
