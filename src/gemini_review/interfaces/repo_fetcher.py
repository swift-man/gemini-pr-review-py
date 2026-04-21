from pathlib import Path
from typing import Protocol

from gemini_review.domain import PullRequest


class RepoFetcher(Protocol):
    """PR 의 head SHA 시점 소스트리를 로컬에 준비하는 추상화.

    구체 구현(`GitRepoFetcher`) 은 partial clone + depth=1 fetch 로 네트워크/디스크
    비용을 최소화하지만, 애플리케이션 계층은 "체크아웃된 루트 경로" 만 알면 됩니다.
    """

    def checkout(self, pr: PullRequest, installation_token: str) -> Path:
        """PR head SHA 기준으로 체크아웃하고 로컬 저장소 루트 경로를 반환합니다.

        설치 토큰은 clone/fetch 시점에 원격 URL 로 주입되고, 반환 후에는 .git/config
        에 토큰이 남지 않도록 원래 URL 로 복구하는 것을 구현체가 책임집니다.
        """
        ...
