import json
import logging
import re

from gemini_review.domain import Finding, ReviewEvent, ReviewResult

logger = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"\{(?:[^{}]|\{[^{}]*\})*\}", re.DOTALL)
# Gemini CLI 는 JSON 을 ```json ... ``` 코드펜스로 감싸 돌려주는 빈도가 높다.
# 선행 스트립 단계에서 양쪽 펜스를 제거해 두면 `_extract_json` 의 "맨앞이 '{'" 빠른 경로에 태울 수 있다.
_CODE_FENCE_OPEN = re.compile(r"^```(?:json|JSON)?\s*", re.MULTILINE)
_CODE_FENCE_CLOSE = re.compile(r"\s*```\s*$", re.MULTILINE)

# 라인 코멘트 body 는 "[Critical]" / "[Major]" / "[Minor]" / "[Suggestion]" 접두사로 시작해야
# 한다 (프롬프트 강제 규약). 여기선 **하드 드롭 없이** 누락/오태그만 WARN 으로 찍어 운영
# 관측성을 확보한다. 실관측 빈도가 높아지면 드롭/정규화로 강화.
_SEVERITY_PREFIX = re.compile(r"^\[(Critical|Major|Minor|Suggestion)\] ")
# 위 정규식과 같은 그룹이지만, 본문 시작 위치에서 **태그 부분만** 캡처해 교체할 때 사용.
_SEVERITY_PREFIX_HEAD = re.compile(r"^\[(Critical|Major|Minor|Suggestion)\] (.*)", re.DOTALL)

# 환각 패턴 — 운영 중 실관측된 표현 위주. Critical/Major 본문에서 발견되면 [Suggestion]
# 으로 자동 강등 (사용자 신고에 따라 등급 신호 인플레이션 방지). 거짓 양성을 줄이려고
# **본문에 등장하는 강한 표지**만 골랐고, 일반 단어("escape", "literal") 만으로는 매칭하지
# 않는다. 새 환각 표현이 또 관찰되면 여기에 누적.
_HALLUCINATION_PATTERNS = (
    "리터럴 'n'",
    "리터럴 \"n\"",
    "리터럴 `n`",
    "literal 'n'",
    'literal "n"',
    "literal `n`",
    "이스케이프 누락",  # "개행 문자 이스케이프가 누락" 류 — escape 오독의 정형 표현
    "@@n",  # 모델이 "@@n 등으로 하드코딩" 같은 식으로 가짜 패턴 인용
    "+xn",  # 동일 카테고리의 가짜 patch 인용
)


def parse_review(
    raw: str,
    *,
    valid_paths: frozenset[str] = frozenset(),
) -> ReviewResult:
    """모델 출력에서 ReviewResult 추출.

    `valid_paths` 가 비어 있지 않으면, 그 집합에 없는 path 를 가진 finding 은 드롭한다
    (path grounding — 모델이 PR 에 존재하지 않는 파일을 지적하는 환각 차단). 빈 집합
    이면 검증을 생략 — 단위 테스트가 path 검증과 무관한 시나리오에서 호출하도록 호환성.
    """
    payload = _extract_json(raw)
    if payload is None:
        logger.warning("gemini output did not contain JSON; falling back to plain text")
        return ReviewResult(
            summary=raw.strip()[:4000] or "Gemini 응답을 파싱하지 못했습니다.",
            event=ReviewEvent.COMMENT,
        )

    event = _parse_event(payload.get("event"))
    findings = tuple(_parse_findings(payload.get("comments"), valid_paths=valid_paths))

    return ReviewResult(
        summary=str(payload.get("summary", "")).strip() or "요약 없음",
        event=event,
        positives=tuple(_as_str_list(payload.get("positives"))),
        improvements=tuple(_as_str_list(payload.get("improvements"))),
        findings=findings,
    )


def _extract_json(text: str) -> dict[str, object] | None:
    stripped = _strip_code_fence(text.strip())
    if stripped.startswith("{"):
        try:
            # json.loads 의 반환 타입은 Any — dict 가 아닌 list/primitive 가 올 수 있으므로
            # 경계에서 명시적으로 좁혀 선언 타입을 지킨다.
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(parsed, dict):
                return parsed

    # Gemini CLI 는 생각 과정을 로그로 찍은 뒤 최종 JSON 을 맨 뒤에 출력할 수 있다.
    # 중간에 `{"note": "..."}` 같은 디버그 JSON 이 끼어도 최종 리뷰 JSON 은 가장 마지막.
    # 따라서 뒤에서부터 훑으며 "summary" 키를 가진 첫 후보를 리뷰 결과로 채택한다.
    candidates = _JSON_BLOCK.findall(text)
    for candidate in reversed(candidates):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "summary" in data:
            return data
    return None


def _strip_code_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    trimmed = _CODE_FENCE_OPEN.sub("", text, count=1)
    trimmed = _CODE_FENCE_CLOSE.sub("", trimmed, count=1)
    return trimmed.strip()


def _parse_event(value: object) -> ReviewEvent:
    if isinstance(value, str):
        upper = value.strip().upper()
        if upper in ReviewEvent.__members__:
            return ReviewEvent[upper]
    return ReviewEvent.COMMENT


def _parse_findings(
    raw: object,
    *,
    valid_paths: frozenset[str] = frozenset(),
) -> list[Finding]:
    if not isinstance(raw, list):
        return []
    out: list[Finding] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "")).strip()
        body = str(item.get("body", "")).strip()
        line = _coerce_line(item.get("line"))
        # 라인 번호가 없는 지적은 PR 인라인 코멘트로 붙을 수 없다. 제품 스펙상 "라인 고정 기술 단위
        # 코멘트"만 인라인 대상이며, 나머지 거시적 지적은 improvements 섹션으로 모델이 분류해야 한다.
        if not path or not body or line is None:
            continue
        # Path grounding — PR 변경 파일 목록에 없는 path 는 모델 환각으로 간주하고 드롭.
        # 사용자 보고 사례 2 (`tests/unit/test_github_app_client.py` 같은 fictional path)
        # 의 직접적 차단. valid_paths 가 비어 있는 호출(테스트 호환) 에선 검증 안 함.
        if valid_paths and path not in valid_paths:
            logger.warning(
                "dropping finding on non-changed path (likely hallucination): "
                "path=%s line=%d body=%r",
                path,
                line,
                body[:120],
            )
            continue
        _warn_if_missing_severity_tag(path, line, body)
        body = _maybe_downgrade_severity(path, line, body)
        out.append(Finding(path=path, line=line, body=body))
    return out


def _maybe_downgrade_severity(path: str, line: int, body: str) -> str:
    """Critical/Major 본문에서 환각 패턴이 감지되면 [Suggestion] 으로 강등.

    환각 기반 Critical/Major 가 PR 차단 신호로 인플레이션되는 것을 막기 위함 (사용자
    신고 사례 1·4). 패턴은 실관측된 표현만 등록 — 거짓 양성을 줄이려고 일반 단어는
    피하고 강한 표지("리터럴 'n'", "이스케이프 누락" 등) 만 매칭한다.

    강등 후 본문 앞에 "(자동 강등: 환각 가능성)" 안내를 붙여 수신자가 이유를 인지할
    수 있게 한다 — silent rewrite 는 더 위험.
    """
    head = _SEVERITY_PREFIX_HEAD.match(body)
    if head is None:
        return body
    severity, rest = head.group(1), head.group(2)
    if severity not in ("Critical", "Major"):
        return body
    lower = body.lower()
    matched = next((p for p in _HALLUCINATION_PATTERNS if p.lower() in lower), None)
    if matched is None:
        return body
    logger.warning(
        "downgrading severity %s -> Suggestion due to hallucination pattern %r: "
        "path=%s line=%d body=%r",
        severity,
        matched,
        path,
        line,
        body[:200],
    )
    return f"[Suggestion] (자동 강등: 환각 가능성, 원래 [{severity}]) {rest}"


def _warn_if_missing_severity_tag(path: str, line: int, body: str) -> None:
    """프롬프트 규약대로 `[Critical|Major|Minor|Suggestion]` 접두사가 없으면 경고 로깅.

    설계 선택: 게시는 그대로 진행하고 로그만 남긴다 (하드 드롭·정규화 안 함). 이유는
    (1) 운영자가 누락 빈도를 먼저 관찰할 근거가 필요하고, (2) 일부 모델 출력에서만
    누락된다면 태그 없는 코멘트도 본문 가치는 있을 수 있어서. 실측으로 높은 비율의
    누락이 보이면 그 때 드롭/정규화 레이어로 강화한다.
    """
    if not _SEVERITY_PREFIX.match(body):
        logger.warning(
            "comment body lacks severity tag prefix (expected one of "
            "[Critical|Major|Minor|Suggestion]): path=%s line=%d body=%r",
            path,
            line,
            body[:120],
        )


def _coerce_line(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        n = int(value)
        return n if n > 0 else None
    return None


def _as_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]
