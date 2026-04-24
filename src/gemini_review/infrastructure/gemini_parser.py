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
    # --- escape 시퀀스 오독 (사용자 신고 사례 1·4) ----------------------------
    # 이 패턴들은 escape 시퀀스 환각의 매우 좁은 표지 — 정상 버그 리포트에서 거의
    # 사용되지 않아 false positive 위험이 낮음. 일반적인 본문 표현 ("불필요한 공백",
    # "command not found" 등) 은 정당한 finding 에서도 흔히 쓰이므로 여기 추가하지
    # 않는다 (codex PR #22 review). phantom whitespace / false CI failure 환각은
    # `SourceGroundedFindingVerifier` 가 디스크 검증으로 잡음 (PR #23, Layer B).
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
# Hot path 마이크로 최적화: finding 마다 `p.lower()` 를 다시 부르지 않도록 모듈 로드
# 시 한 번 (원형, 소문자) 쌍으로 미리 묶어 둔다. 매 호출마다 zip 을 다시 돌리는 비용도
# 같이 제거 (gemini PR #20 review #2). 패턴 자체가 ASCII 라 case-folding 차이는 없다.
_HALLUCINATION_PATTERN_PAIRS = tuple((p, p.lower()) for p in _HALLUCINATION_PATTERNS)

# 등급 → "PR 차단 신호 여부" 매핑. Critical/Major 가 "blocking", Minor/Suggestion 은 권고.
# `_normalize_event` 가 REQUEST_CHANGES 를 약화할지 판단할 때 참조.
_BLOCKING_SEVERITIES = frozenset({"Critical", "Major"})


def parse_review(
    raw: str,
    *,
    valid_paths: frozenset[str] | None = None,
) -> ReviewResult:
    """모델 출력에서 ReviewResult 추출.

    `valid_paths` 가 None 이면 path 검증을 생략 (단위 테스트 호환). 명시적으로
    `frozenset` (빈 집합 포함) 을 주면 그 집합에 없는 path 를 가진 finding 을 드롭한다
    (path grounding — 모델이 PR 에 존재하지 않는 파일을 지적하는 환각 차단). 빈 집합
    의 의미가 "검증 안 함" 과 "전부 드롭" 으로 갈리는 모호함을 None sentinel 로 분리.
    """
    payload = _extract_json(raw)
    if payload is None:
        # **보안 주의** (codex PR #24 review #1+#2): raw 응답에는 모델 입력으로 들어간 PR
        # 전체 코드베이스의 일부가 echo 될 수 있다 (예: 시크릿이 포함된 .env, 인증 정보가
        # 박힌 config). 이런 raw 본문이 (1) 외부 로그 수집기로, (2) GitHub 게시 본문으로
        # 새는 두 경로를 모두 차단:
        #   - 로그: 길이·비어있음 메타만 기록
        #   - 게시 본문: 안전한 고정 안내 + 같은 메타만. raw 본문은 어디에도 노출 X.
        # 진단의 자세한 내용은 모델 측 로그·운영 모니터링으로 별도 조사. 빈 stdout 이라면
        # 엔진의 빈-출력 가드 (fallback chain) 가 먼저 잡았어야 — 여기까지 도달한 raw 의
        # 길이가 0 이면 그 가드가 빠진 회귀.
        logger.warning(
            "gemini output did not contain JSON; falling back to plain text "
            "(raw_length=%d, non_empty=%s). raw content NOT logged for security.",
            len(raw),
            bool(raw.strip()),
        )
        return ReviewResult(
            summary=(
                "Gemini 응답을 JSON 으로 파싱하지 못했습니다. "
                f"(raw_length={len(raw)}, non_empty={bool(raw.strip())}) "
                "응답 본문에 코드·시크릿이 포함될 위험으로 raw 본문은 게시하지 않습니다. "
                "다음 push 가 새 리뷰를 트리거할 때까지 대기하거나 운영자에게 문의하세요."
            ),
            event=ReviewEvent.COMMENT,
        )

    raw_event = _parse_event(payload.get("event"))
    findings = tuple(_parse_findings(payload.get("comments"), valid_paths=valid_paths))
    # 등급 강등/path 드롭 이후 남은 finding 분포로 event 재정합. 모델이 환각 기반
    # [Critical] 을 보고 REQUEST_CHANGES 를 골랐는데 우리가 그걸 [Suggestion] 으로
    # 강등하거나 통째로 드롭했다면, REQUEST_CHANGES 는 더 이상 정당하지 않다.
    event = _normalize_event(raw_event, findings)

    return ReviewResult(
        summary=str(payload.get("summary", "")).strip() or "요약 없음",
        event=event,
        positives=tuple(_as_str_list(payload.get("positives"))),
        improvements=tuple(_as_str_list(payload.get("improvements"))),
        findings=findings,
    )


def _normalize_event(event: ReviewEvent, findings: tuple[Finding, ...]) -> ReviewEvent:
    """필터링/강등 이후 finding 분포로 event 를 정합. **약화 전용** (강화는 안 함).

    원칙: 모델이 COMMENT 를 골랐다면 거기엔 모델만 아는 맥락(예: 본문에는 Critical 코멘트가
    있지만 PR 자체는 WIP 라 차단할 단계가 아님) 이 있을 수 있어 우리가 REQUEST_CHANGES 로
    끌어올리지 않는다. event 와 findings 가 모순될 때는 **더 약한 쪽으로** 맞춘다:

    - **REQUEST_CHANGES + 차단 근거 없음** → COMMENT (아래 "약화 보류 규칙" 통과 시)
    - **APPROVE + 차단급 finding 살아 있음** → COMMENT (codex PR #20 review #4)
    - **APPROVE + 태그 누락 finding 있음** → COMMENT (codex PR #20 review #5)

    ### 태그 누락 finding 의 양방향 보수적 처리 (대칭성)

    파서는 본문 앞에 `[등급]` 접두사 없는 finding 도 드롭하지 않고 WARN 만 찍은 채
    게시한다. 이런 finding 은 "차단 사유가 숨어 있을 수도, 단순 메모일 수도" 있다 —
    어느 쪽인지 우리가 판단할 수 없다. 그러므로 **양방향으로 보수적**:

    - REQUEST_CHANGES 에서: 태그 누락 finding 이 있으면 약화 보류 (차단을 지우면 안 됨)
    - APPROVE 에서: 태그 누락 finding 이 있으면 약화 발동 (승인을 유지하면 안 됨)

    같은 신호의 비대칭 처리는 위험하다 (codex PR #20 review #5): REQUEST_CHANGES 에서는
    "차단일 수 있다" 로 보수적이면서 APPROVE 에서는 "비차단으로 추정" 한다면, 모델이
    태그만 깜빡한 차단 사유 본문을 승인 리뷰로 게시하는 false negative 가 생긴다.
    양쪽 모두 "차단일 가능성을 우대한다" 가 일관된 정책.

    APPROVE + Critical/Major 또는 태그 누락은 모델의 **자기 모순** 이다: "통과시켜라"
    라고 골랐지만 본문에는 차단 가능성이 적힌 상황. COMMENT 로 내려 "본문을 읽어보세요"
    로 자연스레 유도. REQUEST_CHANGES 로 격상은 여전히 안 함 — 차단의 적합성을 판단할
    정보 부족.

    ### REQUEST_CHANGES 약화 보류 규칙 (보수적 우선)

    아래 중 하나라도 만족하면 **약화 보류** — 모델 REQUEST_CHANGES 의도를 존중:

    1. **finding 에 `[Critical]`/`[Major]` 가 살아 있음** — 인라인 차단 사유 명확히 존재.
    2. **태그 누락 finding 이 있음** — 위 대칭성 규칙.

    `improvements` 는 차단 근거로 쓰지 **않는다** (codex PR #20 review #3): 현재 프롬프트
    스키마상 `improvements` 는 라인 고정이 어려운 **권장 개선** 섹션이지 차단 전용
    섹션이 아니다. 비어있지 않다는 이유만으로 약화를 막으면 사소한 개선 한 줄 때문에
    PR 이 잘못 차단되는 false positive 가 발생한다. 차단 신호를 본문 차원에서 표현하려면
    스키마에 `must_fix` 같은 차단 전용 필드를 도입하는 게 옳은 방향 — 별도 작업.
    """
    severities = [_extract_severity(f.body) for f in findings]
    has_blocking = any(sev in _BLOCKING_SEVERITIES for sev in severities)
    missing_tag_count = severities.count(None)

    # APPROVE 자기 모순:
    #   (a) 승인인데 본문에 차단급 finding 이 명시적으로 살아 있음, 또는
    #   (b) 태그 누락 finding 이 있음 — 차단 사유가 숨어 있을 가능성 (대칭성).
    # 둘 다 → COMMENT 로 약화. 양방향 보수적 처리 (codex PR #20 review #5).
    if event == ReviewEvent.APPROVE and (has_blocking or missing_tag_count > 0):
        if has_blocking:
            blocking = [sev for sev in severities if sev in _BLOCKING_SEVERITIES]
            logger.warning(
                "weakening APPROVE -> COMMENT: %d blocking finding(s) (%s) "
                "contradict the approval; posting COMMENT so the body speaks for itself",
                len(blocking),
                ", ".join(sorted(set(blocking))),
            )
        else:
            logger.warning(
                "weakening APPROVE -> COMMENT: %d untagged finding(s) may hide "
                "blocking intent; posting COMMENT for symmetry with REQUEST_CHANGES "
                "policy (untagged is treated as potentially blocking on both sides)",
                missing_tag_count,
            )
        return ReviewEvent.COMMENT

    if event != ReviewEvent.REQUEST_CHANGES:
        return event
    if has_blocking:
        return event
    # 태그 누락 finding 이 있으면 보수적으로 유지. 본문에 차단 사유가 숨어 있을 수 있음.
    if missing_tag_count > 0:
        logger.warning(
            "keeping REQUEST_CHANGES despite no tagged blocking finding: "
            "%d findings lack severity tag and may hide blocking intent",
            missing_tag_count,
        )
        return event
    logger.warning(
        "weakening REQUEST_CHANGES -> COMMENT: no [Critical]/[Major] findings survive "
        "(filter/downgrade dropped them all). %d findings remain.",
        len(findings),
    )
    return ReviewEvent.COMMENT


def _extract_severity(body: str) -> str | None:
    """body 가 `[등급]` 접두사로 시작하면 그 등급 문자열을 반환, 아니면 None."""
    m = _SEVERITY_PREFIX.match(body)
    return m.group(1) if m else None


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
    valid_paths: frozenset[str] | None = None,
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
        # 의 직접적 차단. valid_paths=None 이면 검증 생략 (단위 테스트 호환).
        # 빈 frozenset 을 명시적으로 주면 "PR 에 변경 파일이 0개" 로 간주해 모든 finding
        # 을 드롭한다 — 모호함 분리가 None sentinel 의 핵심.
        if valid_paths is not None and path not in valid_paths:
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
    if severity not in _BLOCKING_SEVERITIES:
        return body
    lower = body.lower()
    # `_HALLUCINATION_PATTERN_PAIRS` 는 모듈 로드 시 (원형, 소문자) 쌍으로 미리 묶여 있어
    # finding 마다 매 패턴 lower() 호출과 zip() 재실행 비용을 모두 제거.
    matched = next(
        (original for original, lowered in _HALLUCINATION_PATTERN_PAIRS if lowered in lower),
        None,
    )
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
