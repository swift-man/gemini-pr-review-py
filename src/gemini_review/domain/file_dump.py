from dataclasses import dataclass, field


@dataclass(frozen=True)
class TokenBudget:
    max_tokens: int

    @staticmethod
    def chars_per_token() -> int:
        return 4

    def fits(self, char_count: int) -> bool:
        return char_count <= self.max_tokens * self.chars_per_token()

    def max_chars(self) -> int:
        return self.max_tokens * self.chars_per_token()


@dataclass(frozen=True)
class FileEntry:
    path: str
    content: str
    size_bytes: int
    is_changed: bool


@dataclass(frozen=True)
class FileDump:
    entries: tuple[FileEntry, ...]
    total_chars: int
    # 모든 제외 파일의 합집합 — 사용자 노출용 (예: budget notice "제외된 파일 수").
    # filter-cut + budget-cut + read 실패 모두 포함. 의미 구분이 필요한 곳은 아래 두 필드 사용.
    excluded: tuple[str, ...] = field(default_factory=tuple)
    # 의도적으로 필터에서 제외된 파일 — binary/lock/image 등 리뷰 대상 자체가 아님.
    # `_changed_missing` 결정에서 "missing 신호" 로 카운트하면 안 됨 (gemini PR #26
    # review #3): 이미지 1개만 변경된 PR 도 강제 fallback 으로 빠지던 회귀.
    filtered_out: tuple[str, ...] = field(default_factory=tuple)
    # 예산 부족으로 잘려 나간 파일 — 원래 review 대상이지만 들어갈 자리 없음.
    # 변경 파일이 여기에 포함되면 fallback 발동의 진짜 트리거.
    budget_excluded: tuple[str, ...] = field(default_factory=tuple)
    exceeded_budget: bool = False
    budget: TokenBudget | None = None
