from dataclasses import dataclass, field

from .finding import Finding, ReviewEvent


@dataclass(frozen=True)
class ReviewResult:
    """Structured review output.

    Three sections are rendered in the top-level review body:
    - 좋은 점 (positives)
    - 개선할 점 (improvements)
    - 기술 단위 코멘트 (findings) — posted as inline, line-anchored comments

    `model` 은 이 리뷰를 생성한 모델 식별자. 설정돼 있으면 본문 푸터로 렌더되고
    None 이면 생략된다. 값의 의미와 어떤 상황에서 채워지는지는 채워 주는 엔진
    구현의 책임 (예: `GeminiCliEngine` 이 fallback 이후 실제 성공한 모델명을 주입).
    """

    summary: str
    event: ReviewEvent
    positives: tuple[str, ...] = field(default_factory=tuple)
    improvements: tuple[str, ...] = field(default_factory=tuple)
    findings: tuple[Finding, ...] = field(default_factory=tuple)
    model: str | None = None

    def render_body(self) -> str:
        parts: list[str] = [self.summary.strip()]
        if self.positives:
            parts.append("\n**좋은 점**")
            parts.extend(f"- {p}" for p in self.positives)
        if self.improvements:
            parts.append("\n**개선할 점**")
            parts.extend(f"- {i}" for i in self.improvements)
        if self.findings:
            parts.append(f"\n_기술 단위 코멘트 {len(self.findings)}건은 각 라인에 별도 표시됩니다._")
        if self.model:
            # 모든 섹션이 끝난 뒤 footer 로 렌더. 구분선(---)으로 본문과 시각적으로 분리.
            parts.append(f"\n---\n_리뷰 생성 모델: `{self.model}`_")
        return "\n".join(parts).strip()
