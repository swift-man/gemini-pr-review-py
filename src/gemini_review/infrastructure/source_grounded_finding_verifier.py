"""모델 finding 의 quoted-text 단언이 실제 소스 라인에 존재하는지 디스크 검증.

사용자 신고 사례 5 (2026-04) 에 대한 후처리 방어:
- 모델이 `"@scope"` 같은 인용을 토큰화 단계에서 `" @scope"` 로 잘못 분해 → 거꾸로
  "원본에 공백 있음" 으로 단언하는 phantom whitespace 환각이 같은 PR 의 연속 push 에
  대해 반복 보고됨.
- 파서 단계의 패턴 강등 (`_HALLUCINATION_PATTERNS`) 은 알려진 표현만 잡지만, 이 검증은
  body 안의 backtick 인용 substring 이 실제 `path:line` 에 있는지 확인 → 신규 표현도
  잡힘. 둘은 보완 관계.
"""
import dataclasses
import logging
import re
from pathlib import Path

from gemini_review.domain import Finding, ReviewResult
from gemini_review.infrastructure.gemini_parser import _normalize_event

logger = logging.getLogger(__name__)

# 본문에 이런 키워드가 있으면 "원본 텍스트에 대한 단언" 일 가능성 높음 → quote 검증 발동.
# 정상적인 권고/제안 본문 (예: "pathlib.Path 를 쓰세요") 은 대개 이 키워드 없음 → 거짓
# 양성 줄임.
#
# 한국어 힌트는 substring 매칭 (한국어는 단어 경계가 없음).
# 영문 힌트는 word-boundary regex 매칭 — `namespacing` 안의 `spacing` 같은 부분 매칭으로
# 검증이 잘못 발동하는 false positive 방지 (codex PR #23 review #5).
_KOREAN_ASSERTION_HINTS = ("공백", "띄어쓰기", "오타")
_ENGLISH_ASSERTION_HINT_RE = re.compile(
    r"\b(?:whitespace|spacing|typo)\b",
    re.IGNORECASE,
)

# body 에서 backtick 인용된 substring 추출 — 모델이 "원본은 이렇다" 단언 시 가장 자주
# 쓰는 형식. 이중/단일 따옴표는 권고에도 자주 등장해 거짓 양성이 많아 일단 backtick 만.
_BACKTICK_QUOTE = re.compile(r"`([^`]+)`")
_SEVERITY_PREFIX_HEAD = re.compile(r"^\[(Critical|Major|Minor|Suggestion)\] (.*)", re.DOTALL)
_BLOCKING_SEVERITIES = frozenset({"Critical", "Major"})


class SourceGroundedFindingVerifier:
    """체크아웃된 repo 디렉터리에서 라인을 읽어 finding 의 quote 단언을 검증."""

    def verify(self, result: ReviewResult, repo_root: Path) -> ReviewResult:
        """phantom quote 단언을 가진 [Critical]/[Major] finding 을 [Suggestion] 으로 강등.

        ### 강등 발동 조건 (모두 만족)

        1. body 가 [Critical] 또는 [Major] 시작
        2. body 에 assertion hint 키워드 (한글: "공백"/"띄어쓰기"/"오타", 영문 word-boundary
           매칭: `whitespace`/`spacing`/`typo`) 포함
        3. body 에 backtick 인용 substring 이 1개 이상
        4. **인용 중 하나라도** path:line 의 raw 라인에 없음 (strict all-match → keep)

        ### 정책 변천 — strict only 로 단순화 (codex PR #23 review #4 → #6)

        초기 lenient ("any-match → keep") 정책은 typo+fix 패턴을 보호했지만 phantom + real
        혼합 본문 ("`usrname` 대신 `\" usrname\"` 사용" 같은) 도 1개 매치로 통과시켜 환각
        [Critical] 우회 경로가 됐다. fix-pattern hint (`→`, `로 수정` 등) 로 lenient 발동을
        좁히려 했으나, codex review #6 의 추가 지적: 같은 fix-pattern hint 가 phantom 본문
        에서도 등장 가능 (예: "`usrname` 대신 `phantom`" 같이 hint 가 들어간 phantom case).
        의미 구별은 NLP 없이 표면 패턴만으로 불가능.

        결정: **strict only**. 모든 backtick 인용이 라인에 매치돼야 통과. typo+fix finding
        도 [Suggestion] 으로 강등되지만 본문/원래 등급은 보존돼 사용자가 직접 판단 가능.
        false positive (정당 finding 강등) 비용을 받아들이고 phantom 차단을 우선.

        강등으로 blocking 분포가 바뀌면 `_normalize_event` 가 event 를 재정합.

        ### I/O 비용

        같은 repo 안에서 여러 finding 이 같은 파일을 가리키는 경우가 흔함 (대형 PR).
        verify() 호출 1회 안에서 파일별 읽은 라인 캐시를 둬 같은 파일을 여러 번 읽지 않음
        (codex PR #23 review #2). 캐시는 호출 단위라 다음 verify() 호출 (다음 PR) 에는
        상태가 새로 시작됨 — 메모리 누수 위험 없음.
        """
        # 호출 단위 파일 라인 캐시. key=path (relative), value=lines list 또는 None (읽기 실패).
        # `None` 도 명시적으로 캐싱해 같은 누락 파일을 여러 번 읽으려 시도하지 않음.
        line_cache: dict[str, list[str] | None] = {}
        new_findings = tuple(
            self._maybe_downgrade(f, repo_root, line_cache) for f in result.findings
        )
        new_event = _normalize_event(result.event, new_findings)
        return dataclasses.replace(result, findings=new_findings, event=new_event)

    def _maybe_downgrade(
        self,
        f: Finding,
        repo_root: Path,
        line_cache: dict[str, list[str] | None],
    ) -> Finding:
        head = _SEVERITY_PREFIX_HEAD.match(f.body)
        if head is None:
            return f
        severity, rest = head.group(1), head.group(2)
        if severity not in _BLOCKING_SEVERITIES:
            return f
        # assertion hint 검사: 키워드 없으면 단순 권고/제안 — 검증 생략 (false positive 방지).
        # 정상 권고 본문 (예: "pathlib.Path 를 쓰세요") 까지 검증하면 인용된 API 이름이
        # 라인에 없다는 이유로 모두 강등돼 신호 가치를 잃는다.
        # 한국어 힌트는 substring, 영문 힌트는 word-boundary regex (codex PR #23 review #5):
        # `namespacing` 안의 `spacing` 같은 부분 매칭으로 검증이 잘못 발동하던 false positive
        # 방지. 영문은 case-insensitive (review #2 도 함께 만족).
        if not _has_assertion_hint(f.body):
            return f
        quotes = _BACKTICK_QUOTE.findall(f.body)
        if not quotes:
            return f
        line, status = _read_source_line(repo_root, f.path, f.line, line_cache)
        # 디스크에서 라인을 못 읽은 케이스의 처리 (codex PR #23 review #3):
        #   - "missing": 변경 파일 목록에는 있지만 체크아웃엔 없음 (삭제된 파일 등)
        #     → 모델이 실제로 본 파일이 아님 → phantom quote 가능성 → 강등
        #   - "out_of_range": 파일은 있지만 line 이 범위 밖
        #     → 모델이 잘못된 라인을 가리킨 환각 → 강등
        #   - "traversal": path traversal 시도 (../../etc/passwd 류)
        #     → 명백히 신뢰 불가 입력 → 강등
        # 이전엔 이 모든 케이스가 silent pass 였음. 모델이 못 본 / 잘못 본 / 악의적 path 의
        # phantom finding 이 [Critical] 그대로 게시됐던 보안+정확성 회귀.
        if line is None:
            self._log_unverifiable_downgrade(severity, f, status)
            return Finding(
                path=f.path,
                line=f.line,
                body=(
                    f"[Suggestion] (자동 강등: {f.path}:{f.line} 의 실제 라인을 "
                    f"검증할 수 없음 [{status}] — 모델이 본 파일이 아닐 가능성, "
                    f"원래 [{severity}]) {rest}"
                ),
            )
        # 매칭 정책: **strict only** (codex PR #23 review #4 → #6 의 최종 조정).
        # 모든 backtick 인용이 라인에 매치돼야 통과. 하나라도 없으면 phantom 의심으로 강등.
        # typo+fix 패턴 ("`old` → `new`") 도 strict 에선 강등되지만 본문 + 원래 등급은
        # 보존돼 사용자가 직접 판단 가능. lenient/fix-pattern 휴리스틱은 phantom + real
        # 혼합 본문 우회를 못 막아 NLP 없이는 정확한 구별 불가하다는 결론.
        matches = [q for q in quotes if q in line]
        if len(matches) == len(quotes):
            return f
        # 매칭 실패 — phantom quote 환각으로 강등
        first_missing = next(q for q in quotes if q not in line)
        logger.warning(
            "downgrading severity %s -> Suggestion: phantom-quote in %s:%d "
            "(missing: %r, total quotes=%d, matched=%d). "
            "assertion-hint keyword triggered verification.",
            severity,
            f.path,
            f.line,
            first_missing,
            len(quotes),
            len(matches),
        )
        return Finding(
            path=f.path,
            line=f.line,
            body=(
                f"[Suggestion] (자동 강등: 인용 텍스트 `{first_missing}` 등이 "
                f"{f.path}:{f.line} 의 실제 라인에 없음 — phantom quote 환각 가능성, "
                f"원래 [{severity}]) {rest}"
            ),
        )

    def _log_unverifiable_downgrade(self, severity: str, f: Finding, status: str) -> None:
        logger.warning(
            "downgrading severity %s -> Suggestion: cannot verify %s:%d (%s); "
            "phantom-source defense (assertion-hint keyword + missing source).",
            severity,
            f.path,
            f.line,
            status,
        )


def _has_assertion_hint(body: str) -> bool:
    """body 에 assertion hint 키워드가 있으면 True — quote 검증 발동 트리거.

    한국어 힌트는 단어 경계 개념이 약해 substring 매칭. 영문 힌트는 word-boundary regex
    매칭 — `namespacing` 안의 `spacing` 같은 부분 매칭으로 검증이 잘못 발동하던 false
    positive 방지 (codex PR #23 review #5). 영문은 case-insensitive (review #2 도 함께).
    """
    if any(hint in body for hint in _KOREAN_ASSERTION_HINTS):
        return True
    return bool(_ENGLISH_ASSERTION_HINT_RE.search(body))


def _read_source_line(
    repo_root: Path,
    path: str,
    line: int,
    line_cache: dict[str, list[str] | None],
) -> tuple[str | None, str]:
    """`repo_root / path` 의 1-based line 번호 라인을 반환.

    Returns:
        (line_text, status) 튜플:
        - 정상: ("실제 라인 텍스트", "ok")
        - 라인 못 읽음: (None, status) — status 는 호출자가 진단 메시지에 사용
            - "missing": 파일이 디스크에 없음 (삭제된 파일 등)
            - "out_of_range": 파일은 있으나 라인 번호 범위 밖
            - "traversal": path 가 repo_root 밖을 가리킴 (../ 등)
            - "io_error": 그 외 IO 오류 (권한, 바이너리 등)

    같은 verify() 호출 안에서 파일별 라인 리스트를 캐싱해 같은 파일을 여러 번 읽지 않음
    (codex PR #23 review #2). path traversal 방어 (gemini PR #23 review): `path` 는 모델
    출력에서 온 신뢰 불가 입력이므로 `..` 등으로 repo_root 밖을 가리킬 수 있다. resolve()
    후 repo_root 의 자식인지 확인 — 아니면 즉시 traversal 로 거부 (캐시도 안 함).
    """
    # Path traversal 방어 — `..` 등 repo_root 를 벗어나는 경로는 거부.
    # `resolve()` 는 symlink 도 따라가 최종 경로를 만든다. is_relative_to 로 봉쇄 검증.
    try:
        resolved_root = repo_root.resolve()
        candidate = (repo_root / path).resolve()
    except OSError:
        return None, "io_error"
    if not candidate.is_relative_to(resolved_root):
        logger.warning(
            "rejected path traversal in finding: path=%r resolved=%s outside repo_root=%s",
            path, candidate, resolved_root,
        )
        return None, "traversal"

    if path in line_cache:
        lines = line_cache[path]
    else:
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
        except OSError:
            lines = None
        line_cache[path] = lines
    if lines is None:
        return None, "missing"
    if line <= 0 or line > len(lines):
        return None, "out_of_range"
    return lines[line - 1], "ok"
