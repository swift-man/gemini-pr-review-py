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
    # PR 의 각 변경 파일에 대해 GitHub 인라인 코멘트가 허용되는 RIGHT-side 라인 집합.
    # `head_sha` 시점에 한 번만 수집해 캐시한다 — 리뷰 생성에 수 분이 걸리는 동안
    # 사용자가 새 커밋을 push 하면 patch 가 갱신되므로, 게시 시점에 다시 fetch 하면
    # 모델 finding 의 라인 번호와 불일치하는 race condition 이 발생.
    # 빈 frozenset 은 binary 파일·삭제 파일·GitHub truncate 등 인라인 불가 케이스.
    # 형식: `((path, frozenset(lines)), ...)` — frozen dataclass 호환을 위해 dict 대신 tuple.
    addable_lines: tuple[tuple[str, frozenset[int]], ...] = field(default_factory=tuple)

    def addable_lines_by_path(self) -> dict[str, frozenset[int]]:
        """`addable_lines` 튜플을 조회 친화적인 dict 로 변환."""
        return dict(self.addable_lines)
