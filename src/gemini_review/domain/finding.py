from dataclasses import dataclass
from enum import Enum


class ReviewEvent(str, Enum):
    COMMENT = "COMMENT"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    APPROVE = "APPROVE"


@dataclass(frozen=True)
class Finding:
    """라인에 고정되는 한국어 기술 단위 코멘트.

    `line` 은 필수 — 이 봇은 PR 오른쪽(RIGHT) 쪽 특정 줄 번호에 붙는 코멘트만
    게시하므로, 리뷰어가 해당 라인에서 바로 볼 수 있어야 한다.
    """

    path: str
    line: int
    body: str
