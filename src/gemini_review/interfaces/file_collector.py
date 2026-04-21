from pathlib import Path
from typing import Protocol

from gemini_review.domain import FileDump, TokenBudget


class FileCollector(Protocol):
    """체크아웃된 소스트리를 "프롬프트에 올릴 파일 묶음" 으로 만드는 추상화.

    필터 규칙 / 우선순위 / 토큰 예산은 구체 구현의 책임이고, 상위 계층은 결과인
    `FileDump` 만 소비합니다. 테스트에서는 간단한 in-memory Fake 로 교체할 수 있습니다.
    """

    def collect(
        self,
        root: Path,
        changed_files: tuple[str, ...],
        budget: TokenBudget,
    ) -> FileDump:
        """변경 파일을 최우선으로 하여 예산 한도까지 파일을 모읍니다.

        예산을 넘겨 변경 파일 중 일부가 빠진 경우 `FileDump.exceeded_budget=True` 를
        설정해 호출자가 "리뷰 수행 vs. 안내 코멘트" 분기 결정을 내릴 수 있게 합니다.
        """
        ...
