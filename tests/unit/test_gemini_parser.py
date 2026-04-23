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


# --- Path grounding (사용자 신고 사례 2 차단) -------------------------------


def test_parse_drops_finding_on_path_not_in_valid_paths(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`valid_paths` 가 주어지면 그 집합에 없는 path 는 환각으로 간주하고 드롭한다.

    실관측 사례: 모델이 PR 에 존재하지 않는 `tests/unit/test_github_app_client.py` 를
    "httpx 리팩터링 후 urllib 잔존" 같은 강한 주장과 함께 지적. path 검증으로 이런
    환각이 게시 자체에 도달하지 않도록 차단.
    """
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "src/real.py", "line": 10, "body": "[Major] 실제 변경 파일"},
        {"path": "tests/imaginary.py", "line": 1, "body": "[Critical] 가짜 파일"}
      ]
    }
    """
    with caplog.at_level(logging.WARNING):
        result = parse_review(raw, valid_paths=frozenset({"src/real.py"}))

    paths = [f.path for f in result.findings]
    assert paths == ["src/real.py"], "valid_paths 밖의 finding 은 드롭돼야 함"
    drop_warnings = [r for r in caplog.records if "non-changed path" in r.getMessage()]
    assert len(drop_warnings) == 1
    assert "tests/imaginary.py" in drop_warnings[0].getMessage()


def test_parse_skips_path_grounding_when_valid_paths_empty() -> None:
    """`valid_paths` 가 빈 frozenset 이면 검증 생략 — 단위 테스트 호환성 보장."""
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "any/path.py", "line": 1, "body": "[Minor] x"}
      ]
    }
    """
    result = parse_review(raw)  # valid_paths 미지정 → 검증 안 함
    assert len(result.findings) == 1
    assert result.findings[0].path == "any/path.py"


# --- Severity downgrade on hallucination (사용자 신고 사례 1·4 차단) --------


def test_parse_downgrades_critical_to_suggestion_on_literal_n_hallucination(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """실관측된 escape 환각 표현이 [Critical]/[Major] 본문에 있으면 [Suggestion] 으로 강등.

    회귀 방지: 사용자 신고 사례 1 (`\\n` 을 literal `n` 으로 잘못 읽음) 가 [Major]
    등급으로 게시되어 false positive PR 차단으로 이어진 것을 방지.
    """
    raw = """
    {
      "summary": "ok",
      "event": "REQUEST_CHANGES",
      "comments": [
        {"path": "tests/x.py", "line": 5, "body": "[Major] 테스트 픽스처에서 개행 문자 이스케이프 누락되어 리터럴 'n' 으로 하드코딩됨"}
      ]
    }
    """
    with caplog.at_level(logging.WARNING):
        result = parse_review(raw)

    assert len(result.findings) == 1
    body = result.findings[0].body
    # 강등됐는지: 새 본문이 [Suggestion] 으로 시작
    assert body.startswith("[Suggestion]")
    # 원래 등급이 무엇이었는지 안내 문구로 노출 — silent rewrite 방지
    assert "자동 강등" in body
    assert "원래 [Major]" in body
    # 강등 사실이 WARN 으로 로깅됨 — 운영자가 빈도 추적 가능
    downgrade_warnings = [r for r in caplog.records if "downgrading severity" in r.getMessage()]
    assert len(downgrade_warnings) == 1


def test_parse_does_not_downgrade_legitimate_critical_without_hallucination_marker() -> None:
    """환각 패턴이 없는 정상 [Critical] 은 유지 — 거짓 양성 방지.

    회귀 방지: hallucination 패턴 매칭이 너무 공격적이면 정당한 Critical 이 강등돼
    리뷰 시스템 신호 가치가 오히려 떨어진다. 일반 단어("escape") 만으로는 매칭하지
    않고, 실관측된 강한 표지("리터럴 'n'" 등) 만 잡아야 한다.
    """
    raw = """
    {
      "summary": "ok",
      "event": "REQUEST_CHANGES",
      "comments": [
        {"path": "src/a.py", "line": 10, "body": "[Critical] sys.exit(1) 호출이 uvicorn 을 종료시켜 진행 중인 다른 리뷰 유실. 일반 escape 처리 관련 함수임"}
      ]
    }
    """
    result = parse_review(raw)
    assert len(result.findings) == 1
    assert result.findings[0].body.startswith("[Critical]"), "정상 Critical 은 그대로 유지"
    assert "자동 강등" not in result.findings[0].body


def test_parse_does_not_downgrade_minor_or_suggestion() -> None:
    """이미 [Minor]/[Suggestion] 인 본문에 환각 패턴이 있어도 강등 안 함 (의미 없음)."""
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "x.py", "line": 1, "body": "[Minor] 이스케이프 누락 같은 가능성"},
        {"path": "y.py", "line": 2, "body": "[Suggestion] literal 'n' 처리 검토"}
      ]
    }
    """
    result = parse_review(raw)
    assert result.findings[0].body.startswith("[Minor]")
    assert result.findings[1].body.startswith("[Suggestion]")
    assert all("자동 강등" not in f.body for f in result.findings)
