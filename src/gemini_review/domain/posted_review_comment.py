from dataclasses import dataclass


@dataclass(frozen=True)
class PostedReviewComment:
    """이미 PR 에 게시된 라인 고정 인라인 리뷰 코멘트의 도메인 표현.

    GitHub `/pulls/{n}/comments` 응답에서 두 후처리 layer 가 필요로 하는 최소 필드:

    - **Layer D — CrossPrFindingDeduper** (PR #25): `(path, line, body)` 시그니처 매칭
      으로 같은 finding 의 반복 게시를 강등.

    - **Layer E — DiffBasedResolutionChecker** (PR #28): `comment_id` 로 대댓글 게시,
      `original_commit_id` / `original_line` 으로 코멘트 anchor 시점 본문, `commit_id` /
      `line` 으로 현재 본문 비교. `in_reply_to_id` 로 본 봇이 이미 대댓글을 단 코멘트는
      중복 회피.

    ### `commit_id`/`line` vs `original_commit_id`/`original_line`

    GitHub 는 PR 진행에 따라 review comment 의 위치를 자동 추적:
    - **`commit_id`/`line`**: 현재 head 시점의 위치. PR 이 진행되며 라인 위쪽에 새 코드
      가 추가되면 GitHub 가 line 을 +N 으로 자동 조정해 같은 라인을 가리키도록 갱신.
      코멘트 thread 가 어디 anchored 됐는지 보여주는 GitHub UI 의 기준.
    - **`original_commit_id`/`original_line`**: 코멘트가 처음 게시된 시점의 SHA / line.
      GitHub 가 추적을 갱신해도 보존됨 — Layer E 가 "anchor 시점 본문 vs 현재 본문"
      을 정확히 비교하려면 이 두 필드가 필요 (gemini PR #28 review #1).

    `line` 이 None 인 경우 (line shift 가 너무 커서 추적 실패 = outdated) 는 인프라
    매핑 단계에서 드롭 — anchor 가 사라진 코멘트는 신뢰할 수 없는 비교 key.
    """

    comment_id: int
    commit_id: str
    path: str
    line: int
    body: str
    # 본 봇 자신이 게시한 대댓글이면 부모 코멘트 id. None 이면 top-level 코멘트.
    # Layer E 가 "이미 대댓글 단 코멘트" 를 구분해 중복 reply 회피하는 데 사용.
    in_reply_to_id: int | None = None
    # 코멘트가 처음 게시된 시점의 SHA / line. Layer E 의 diff 비교 anchor.
    # 매핑 단계에서 GitHub 응답에 없으면 `commit_id`/`line` 으로 fallback (안전).
    original_commit_id: str = ""
    original_line: int = 0
