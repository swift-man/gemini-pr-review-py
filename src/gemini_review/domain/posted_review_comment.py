from dataclasses import dataclass


@dataclass(frozen=True)
class PostedReviewComment:
    """이미 PR 에 게시된 라인 고정 인라인 리뷰 코멘트의 도메인 표현.

    GitHub `/pulls/{n}/comments` 응답에서 dedup 검사에 필요한 최소 필드만 추려 낸다.
    Layer D (cross-PR finding dedup) 가 본 봇이 같은 PR 의 이전 push 에서 게시한
    `(path, line, body)` 와 새 finding 을 비교해 중복 [Critical]/[Major] 를
    [Suggestion] 으로 강등할 때 사용한다.

    `line` 이 None 인 경우 (force-push 후 outdated 코멘트) 는 인프라 매핑 단계에서
    드롭 — 라인 anchor 가 사라진 코멘트는 신뢰할 수 없는 dedup key 라서 비교에서 제외.
    """

    path: str
    line: int
    body: str
