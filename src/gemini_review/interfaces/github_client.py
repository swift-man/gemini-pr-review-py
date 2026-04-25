from typing import Protocol

from gemini_review.domain import PostedReviewComment, PullRequest, RepoRef, ReviewResult


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

    def reply_to_review_comment(
        self,
        pr: PullRequest,
        comment_id: int,
        body: str,
    ) -> None:
        """기존 라인 고정 리뷰 코멘트에 대댓글을 게시합니다.

        Layer E (`DiffBasedResolutionChecker`) 가 본 봇이 이전에 게시한 [Critical]/
        [Major] finding 의 대상 라인이 새 push 에서 변경됐을 때 "수정 여부 확인" 대댓글을
        다는 데 사용. GitHub 의 `POST /repos/{}/{}/pulls/{n}/comments/{cid}/replies`
        엔드포인트.
        """
        ...

    def list_self_review_comments(
        self, pr: PullRequest
    ) -> tuple[PostedReviewComment, ...]:
        """본 GitHub App 이 이전에 PR 에 게시한 라인 고정 인라인 리뷰 코멘트 목록을 반환합니다.

        Layer D (cross-PR finding dedup) 가 같은 PR 의 이전 push 에서 본 봇이 직접
        게시했던 코멘트를 dedup key 로 쓰기 위해 사용. 다른 사람·다른 봇의 코멘트는
        제외해야 의미 있는 비교가 됨 — 본 봇이 무시되는지를 봐야 하는 신호이므로.

        식별 기준은 `performed_via_github_app.id == self._app_id` (봇 이름 변경에 강건).
        force-push 로 anchor 가 깨진 outdated 코멘트(`line == null`) 는 제외 — 라인
        매칭 dedup 에 쓸 수 없음.
        """
        ...

    def get_installation_token(self, installation_id: int) -> str:
        """App JWT 로 설치 토큰을 발급/캐싱해 반환합니다."""
        ...
