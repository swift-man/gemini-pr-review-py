from pathlib import Path
from typing import Protocol

from gemini_review.domain import PullRequest


class FindingResolutionChecker(Protocol):
    """본 봇이 이전에 게시한 라인 코멘트의 대상 라인이 새 push 에서 수정됐는지 확인 → 대댓글.

    Layer E 의 책임 — 일방적으로 라인 코멘트를 다는 것에서 끝나지 않고, 후속 push 에서
    수정 여부를 추적해 동일 코멘트 thread 에 follow-up 대댓글을 게시. 메인테이너 입장
    에서는 "내가 수정한 게 봇이 인지했는지" 가 명확해져 review 흐름의 conversation
    품질이 올라간다.

    Layer D (`CrossPrFindingDeduper`) 와의 분담:
    - Layer D: **새 push** 의 finding 이 이전과 동일하면 (메인테이너가 무시한 신호로 보고)
      [Suggestion] 으로 강등.
    - Layer E: **이전 push** 의 코멘트가 가리키는 라인이 새 push 에서 변경됐으면 (메인
      테이너가 처리한 신호로 보고) 그 코멘트 thread 에 "수정 확인 요청" 대댓글.

    구현체 (`DiffBasedResolutionChecker`) 는 로컬 git checkout 에서 두 commit 의 라인
    내용을 비교하는 단순 diff 기반 판정. 다른 전략 (모델 기반 의도 일치 판정 등) 으로
    교체 가능하도록 Protocol 로 분리.
    """

    def check_resolutions(self, pr: PullRequest, repo_root: Path) -> None:
        """side effect 만 — 대댓글 게시. return 값 없음.

        graceful degrade 필수: 어떤 실패도 리뷰 게시 흐름 (이미 끝난) 에 영향 주면 안 됨.
        한 코멘트 처리 실패가 다음 코멘트 처리를 막아도 안 됨 — 각 코멘트는 독립 처리.
        """
        ...
