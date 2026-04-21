from gemini_review.domain import Finding, ReviewEvent, ReviewResult


def test_render_body_includes_three_sections() -> None:
    result = ReviewResult(
        summary="요약입니다.",
        event=ReviewEvent.COMMENT,
        positives=("Protocol 기반 DIP",),
        improvements=("계층 경계 강화",),
        findings=(Finding(path="a.py", line=1, body="functools.cache를 고려하세요."),),
    )
    body = result.render_body()
    assert body.startswith("요약입니다.")
    assert "**좋은 점**" in body
    assert "- Protocol 기반 DIP" in body
    assert "**개선할 점**" in body
    assert "- 계층 경계 강화" in body
    assert "기술 단위 코멘트 1건" in body


def test_render_body_omits_empty_sections() -> None:
    result = ReviewResult(summary="요약", event=ReviewEvent.COMMENT)
    body = result.render_body()
    assert body == "요약"


def test_render_body_without_findings_does_not_mention_inline_comments() -> None:
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.COMMENT,
        positives=("좋음",),
    )
    body = result.render_body()
    assert "기술 단위 코멘트" not in body


def test_render_body_appends_model_footer_when_model_is_set() -> None:
    """모델명이 설정돼 있으면 본문 마지막에 구분선과 함께 푸터로 렌더.

    fallback 체인 발동 시 어떤 모델이 실제로 리뷰를 만들었는지 PR 본문에서
    바로 알 수 있어야 한다.
    """
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.COMMENT,
        positives=("좋음",),
        model="gemini-2.5-pro",
    )
    body = result.render_body()

    assert body.endswith("_리뷰 생성 모델: `gemini-2.5-pro`_")
    # 본문과 시각적으로 분리되는 구분선이 있어야 푸터로 읽힌다.
    assert "---" in body
    # 모델 푸터가 기존 섹션 뒤에 와야 한다 (중간에 끼어들면 안 됨).
    assert body.index("**좋은 점**") < body.index("gemini-2.5-pro")


def test_render_body_omits_model_footer_when_model_is_none() -> None:
    """모델명이 없으면(기본값) 푸터를 찍지 않고 기존 동작을 유지한다 — 하위 호환 보장."""
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.COMMENT,
    )
    body = result.render_body()

    assert "리뷰 생성 모델" not in body
    assert "---" not in body
    assert body == "요약"
