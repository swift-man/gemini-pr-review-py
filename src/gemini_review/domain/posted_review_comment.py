from dataclasses import dataclass


@dataclass(frozen=True)
class PostedReviewComment:
    """이미 PR 에 게시된 라인 고정 인라인 리뷰 코멘트의 도메인 표현.

    GitHub `/pulls/{n}/comments` 응답에서 두 후처리 layer 가 필요로 하는 최소 필드:

    - **Layer D — CrossPrFindingDeduper** (PR #25): `(path, line, body)` 시그니처 매칭
      으로 같은 finding 의 반복 게시를 강등.

    - **Layer E — DiffBasedResolutionChecker** (PR #28): `comment_id` 로 대댓글 게시,
      `commit_id` 로 댓글 시점 SHA 와 현재 head 비교, `in_reply_to_id` 로 본 봇이 이미
      대댓글을 단 코멘트는 중복 회피.

    `line` 이 None 인 경우 (force-push 후 outdated 코멘트) 는 인프라 매핑 단계에서
    드롭 — 라인 anchor 가 사라진 코멘트는 신뢰할 수 없는 dedup key 라서 비교에서 제외.
    """

    comment_id: int
    commit_id: str
    path: str
    line: int
    body: str
    # 본 봇 자신이 게시한 대댓글이면 부모 코멘트 id. None 이면 top-level 코멘트.
    # Layer E 가 "이미 대댓글 단 코멘트" 를 구분해 중복 reply 회피하는 데 사용.
    in_reply_to_id: int | None = None
