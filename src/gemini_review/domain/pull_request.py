from dataclasses import dataclass, field


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class PullRequest:
    repo: RepoRef
    number: int
    title: str
    body: str
    head_sha: str
    head_ref: str
    base_sha: str
    base_ref: str
    clone_url: str
    changed_files: tuple[str, ...]
    installation_id: int
    is_draft: bool
    # `git fetch origin <fetch_ref>` 에 넘길 ref. 정상 PR 에서는 `head_sha` 문자열이지만,
    # fork 가 삭제된 경우 (clone_url 이 base fallback 일 때) `refs/pull/{number}/head` 로
    # 세팅돼 GitRepoFetcher 가 base repo 에서 PR 스냅샷을 받을 수 있게 한다.
    # 필드 기본값은 빈 문자열 — 생성 시점에 반드시 채워져야 함 (테스트 빌더가 실수로
    # 누락하면 post-init 에서 발견). 빈 값 허용은 GitRepoFetcher 호환성을 위해 head_sha
    # 로 자연 fallback 하도록 런타임에서 처리.
    fetch_ref: str = ""
    # PR 의 각 변경 파일에 대해 GitHub 인라인 코멘트가 허용되는 RIGHT-side 라인 집합.
    # `head_sha` 시점에 한 번만 수집해 캐시한다 — 리뷰 생성에 수 분이 걸리는 동안
    # 사용자가 새 커밋을 push 하면 patch 가 갱신되므로, 게시 시점에 다시 fetch 하면
    # 모델 finding 의 라인 번호와 불일치하는 race condition 이 발생.
    # 빈 frozenset 은 binary 파일·삭제 파일·GitHub truncate 등 인라인 불가 케이스.
    # 형식: `((path, frozenset(lines)), ...)` — frozen dataclass 호환을 위해 dict 대신 tuple.
    addable_lines: tuple[tuple[str, frozenset[int]], ...] = field(default_factory=tuple)

    def effective_fetch_ref(self) -> str:
        """실제 `git fetch` 에 사용할 ref — `fetch_ref` 가 비어 있으면 `head_sha` 로 fallback.

        이전 코드와의 호환성을 위해 `fetch_ref` 를 비워둔 채 PullRequest 를 만드는 호출부가
        있을 수 있으므로, 명시적 getter 에서 기본값을 복원해 `GitRepoFetcher` 가 항상 정상
        동작하도록 한다. 새 호출부 (`fetch_pull_request`) 는 항상 둘 중 하나를 명시 세팅.
        """
        return self.fetch_ref or self.head_sha

    def addable_lines_by_path(self) -> dict[str, frozenset[int]]:
        """`addable_lines` 튜플을 조회 친화적인 dict 로 변환."""
        return dict(self.addable_lines)
