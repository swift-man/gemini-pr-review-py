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


def parse_review(raw: str) -> ReviewResult:
    payload = _extract_json(raw)
    if payload is None:
        logger.warning("gemini output did not contain JSON; falling back to plain text")
        return ReviewResult(
            summary=raw.strip()[:4000] or "Gemini 응답을 파싱하지 못했습니다.",
            event=ReviewEvent.COMMENT,
        )

    event = _parse_event(payload.get("event"))
    findings = tuple(_parse_findings(payload.get("comments")))

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
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

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


def _parse_findings(raw: object) -> list[Finding]:
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
        out.append(Finding(path=path, line=line, body=body))
    return out


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
