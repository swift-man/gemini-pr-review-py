import logging

import pytest

from gemini_review.domain import ReviewEvent
from gemini_review.infrastructure.gemini_parser import parse_review


def test_parse_strict_json() -> None:
    raw = """
    {
      "summary": "전반적으로 구조가 깔끔합니다.",
      "event": "COMMENT",
      "positives": ["Protocol을 통한 DIP 적용"],
      "improvements": ["도메인 계층과 인프라 계층의 경계를 더 명확히"],
      "comments": [
        {"path": "src/a.py", "line": 12, "body": "pathlib.Path를 사용하면 경로 조합이 안전해집니다."}
      ]
    }
    """
    result = parse_review(raw)
    assert result.summary.startswith("전반적으로")
    assert result.event == ReviewEvent.COMMENT
    assert result.positives == ("Protocol을 통한 DIP 적용",)
    assert result.improvements == ("도메인 계층과 인프라 계층의 경계를 더 명확히",)
    assert len(result.findings) == 1
    assert result.findings[0].path == "src/a.py"
    assert result.findings[0].line == 12


def test_parse_strips_markdown_code_fence() -> None:
    # Gemini CLI 가 자주 취하는 출력 형태: ```json ... ``` 로 감싼 JSON.
    raw = (
        "```json\n"
        '{"summary": "펜스 제거 후 파싱", "event": "COMMENT", "comments": []}\n'
        "```"
    )
    result = parse_review(raw)
    assert result.summary == "펜스 제거 후 파싱"
    assert result.event == ReviewEvent.COMMENT


def test_parse_picks_last_valid_json_when_reasoning_precedes() -> None:
    raw = (
        "사고 과정: 먼저 파일을 확인...\n"
        '{"note": "intermediate"}\n'
        'Final:\n'
        '{"summary": "최종 리뷰", "event": "REQUEST_CHANGES", "comments": []}'
    )
    result = parse_review(raw)
    assert result.summary == "최종 리뷰"
    assert result.event == ReviewEvent.REQUEST_CHANGES


def test_parse_fallbacks_to_plain_text_when_no_json() -> None:
    result = parse_review("그냥 평문 응답입니다.")
    assert "평문" in result.summary
    assert result.event == ReviewEvent.COMMENT


def test_parse_drops_findings_without_valid_line() -> None:
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "", "line": 1, "body": "empty path"},
        {"path": "src/a.py", "line": "bad", "body": "invalid line"},
        {"path": "src/b.py", "body": "no line — dropped"},
        {"path": "src/c.py", "line": 0, "body": "zero line — dropped"},
        {"path": "src/d.py", "line": 5, "body": "valid"}
      ]
    }
    """
    result = parse_review(raw)
    paths = [f.path for f in result.findings]
    assert paths == ["src/d.py"]
    assert result.findings[0].line == 5


def test_parse_warns_when_comment_body_lacks_severity_prefix(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Severity 태그 누락 시 드롭하지 않고 WARN 로 관측. 회귀 방지.

    정책 이유(기록 목적으로): 현재는 "프롬프트로 강제, 파서는 관측만" 단계. 모델이
    접두사를 생략해도 코멘트 본문 자체엔 가치가 있을 수 있어 하드 드롭하지 않는다.
    운영 로그에서 WARN 빈도가 높게 관찰되면 그 때 드롭/정규화로 강화.
    """
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "src/a.py", "line": 10, "body": "[Critical] 태그 있음 — 경고 없음"},
        {"path": "src/b.py", "line": 20, "body": "태그 없는 본문 — 경고 대상"},
        {"path": "src/c.py", "line": 30, "body": "[Info] 임의 태그 — 경고 대상"}
      ]
    }
    """
    with caplog.at_level(logging.WARNING):
        result = parse_review(raw)

    # 세 건 모두 Finding 으로 살아남아야 함 — 드롭 안 함
    assert len(result.findings) == 3

    # 태그 없는 것과 임의 태그, 두 건에 대해 WARN 이 찍혀야 함
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    warning_text = " | ".join(r.getMessage() for r in warnings)
    assert "src/b.py" in warning_text, "태그 없는 코멘트에 대한 WARN 누락"
    assert "src/c.py" in warning_text, "임의 태그(Info) 코멘트에 대한 WARN 누락"
    assert "severity tag" in warning_text
    # 정상 태그엔 WARN 이 없어야 — 거짓 양성 방지
    assert "src/a.py" not in warning_text


def test_parse_accepts_all_four_severity_tiers_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """4개 허용 등급 각각이 정상 통과하는지 고정."""
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "a.py", "line": 1, "body": "[Critical] x"},
        {"path": "b.py", "line": 2, "body": "[Major] y"},
        {"path": "c.py", "line": 3, "body": "[Minor] z"},
        {"path": "d.py", "line": 4, "body": "[Suggestion] w"}
      ]
    }
    """
    with caplog.at_level(logging.WARNING):
        result = parse_review(raw)

    assert len(result.findings) == 4
    severity_warnings = [r for r in caplog.records if "severity tag" in r.getMessage()]
    assert severity_warnings == [], "정상 태그엔 severity 관련 WARN 이 없어야 한다"
