from dataclasses import dataclass, field

from .finding import Finding, ReviewEvent


@dataclass(frozen=True)
class ReviewResult:
    """구조화된 리뷰 출력물.

    리뷰 본문 상단에는 세 섹션이 렌더된다:
    - 좋은 점 (positives)
    - 개선할 점 (improvements)
    - 기술 단위 코멘트 (findings) — 라인에 고정된 인라인 코멘트로 게시

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

    def render_body(self, *, surface_findings: tuple[Finding, ...] = ()) -> str:
        """리뷰 본문 마크다운을 렌더.

        `surface_findings` 는 GitHub diff 범위 밖이라 인라인으로 게시할 수 없는
        finding 들 — 호출자(`GitHubAppClient.post_review`) 가 사전 분할로 골라낸다.
        이 값이 비어 있지 않으면 본문 끝에 **드롭된 라인 지적** 섹션을 추가해
        `path:line — body` 형태로 나열한다. body 는 이미 `[등급]` 접두사를 포함
        (PR #13 규약) 하고 있어 추가 가공 없이 그대로 노출한다.

        **계약** (호출자 책임):
        - `surface_findings` 는 `self.findings` 의 **부분집합** 이어야 한다. 인라인
          카운트 안내는 `len(self.findings) - len(surface_findings)` 로 자동 계산되므로,
          외부 finding 을 임의로 넣으면 카운트가 음수·과다 표시된다.
        - 순서는 caller 가 의미 있게 정렬해 넘긴다 (보통 모델 출력 원순서 보존).

        finding 정보를 잃지 않으면서도 GitHub 의 인라인 룰을 어기지 않는 절충 — PR
        수신자가 본문 한 곳에서 "정상 인라인 N개 + 본문 surface M개" 모두 확인 가능.
        """
        parts: list[str] = [self.summary.strip()]
        if self.positives:
            parts.append("\n**좋은 점**")
            parts.extend(f"- {p}" for p in self.positives)
        if self.improvements:
            parts.append("\n**개선할 점**")
            parts.extend(f"- {i}" for i in self.improvements)

        # 인라인으로 살아남는 findings 안내 — surface 와 별개
        inline_count = len(self.findings) - len(surface_findings)
        if inline_count > 0:
            parts.append(
                f"\n_기술 단위 코멘트 {inline_count}건은 각 라인에 별도 표시됩니다._"
            )

        if surface_findings:
            parts.append(
                f"\n_(주: 다음 {len(surface_findings)}개 코멘트는 PR diff 범위 밖이라 "
                "본문에 모았습니다.)_"
            )
            parts.append("\n**드롭된 라인 지적**")
            for f in surface_findings:
                parts.append(f"- `{f.path}:{f.line}` — {f.body}")

        if self.model:
            # 모든 섹션이 끝난 뒤 footer 로 렌더. 구분선(---)으로 본문과 시각적으로 분리.
            parts.append(f"\n---\n_리뷰 생성 모델: `{self.model}`_")
        return "\n".join(parts).strip()
