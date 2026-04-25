"""Unified diff 파서 — GitHub Reviews API 가 인라인 코멘트를 받아주는 라인 집합 계산.

GitHub 의 검증 룰: `comments[].line` 이 **이 PR diff 안에 있는** 라인이어야 한다
(추가된 `+` 라인이거나 hunk 안의 context ` ` 라인). 이 룰을 어기면 422 가 나며
bulk 등록이 통째로 거부된다.

이 모듈은 `/repos/{owner}/{repo}/pulls/{n}/files` 가 돌려주는 각 파일의 `patch`
필드를 파싱해서 "RIGHT(new) 사이드 기준으로 코멘트 가능한 라인 번호 집합" 을
반환한다. 이 집합으로 finding 을 사전 필터하면 422 자체가 발생하지 않는다.
"""

import re

# unified diff hunk 헤더: `@@ -old_start,old_count +new_start,new_count @@ context...`
# 우리는 RIGHT(new) 사이드 라인 번호만 필요해서 +new_start 그룹만 캡처한다.
# `,new_count` 부분은 줄 1개일 때 생략될 수 있어 `(?:,\d+)?` 로 옵션 처리.
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def addable_lines_from_patch(patch: str | None) -> set[int]:
    """RIGHT(new) 파일 기준으로 GitHub 인라인 코멘트가 허용되는 라인 번호 집합.

    포함:
    - `+` 로 시작하는 추가 라인 (단, `+++` 헤더는 제외)
    - 한 hunk 안의 context 라인 (` ` 로 시작) — GitHub 도 hunk 범위 안의 변경 안 된
      라인엔 코멘트 허용

    제외:
    - `-` 로 시작하는 제거 라인 (RIGHT 사이드에는 존재하지 않음)
    - hunk 바깥 (전혀 변경 안 된 영역)

    `patch` 가 None 이거나 빈 문자열이면 빈 집합 반환 — binary 파일·삭제 파일·
    GitHub 가 truncate 한 거대 파일 등에서 일어남.
    """
    if not patch:
        return set()

    addable: set[int] = set()
    new_line_no = 0
    for line in patch.splitlines():
        if line.startswith("@@"):
            match = _HUNK_HEADER.match(line)
            if match:
                # hunk 시작 직전 위치로 세팅. 첫 번째 +/space 라인을 만나면 +1 되어
                # 정확한 시작 라인 번호가 된다.
                new_line_no = int(match.group(1)) - 1
        elif line.startswith("+++ ") or line.startswith("--- "):
            # diff 파일 헤더 (`+++ b/path`, `--- a/path`, `+++ /dev/null`) — 본문 아님.
            # **trailing space 까지 매칭** (codex PR #26 review #5): 공백 없는 `+++X`/`---X`
            # 는 content (`++X`/`--X` 가 +/- diff marker 와 만난 형태) 로 처리해야 함.
            # 예: JS `++counter` 추가 → diff line `+++counter` 는 본문 추가 라인이지
            # file header 가 아님. 이전 `startswith("+++")` 만 검사하면 RIGHT 라인
            # 카운터가 안 증가해 후속 addable 가 모두 -1 어긋남 → 422 회귀.
            # `+++` 가 `+` 보다 먼저 검사돼야 아래 분기에서 헤더가 추가 라인으로 잘못
            # 분류되지 않는다 (순서 의존성). content `+++X` 는 trailing space 가 없어
            # 이 분기에서 fall-through 후 `+/space` 분기로 정상 처리됨.
            continue
        elif line.startswith(("+", " ")):
            # 추가(`+`) 와 hunk 안 context(` `) 모두 RIGHT 사이드에 존재하므로
            # 새 파일 라인 카운터를 +1 하고 addable 에 추가 (GitHub 도 둘 다 허용).
            new_line_no += 1
            addable.add(new_line_no)
        # 그 외 (`-` 제거 라인, `\ No newline at end of file` 메타) 는 RIGHT 사이드에
        # 영향 없음 — 카운터를 옮기지 않고 통과.
    return addable


def format_patch_with_line_numbers(patch: str | None) -> str:
    """Patch 의 각 라인을 RIGHT-side 라인 번호로 annotate 한 사람·LLM 친화 포맷.

    Diff-only fallback 리뷰에서 LLM 이 `comments[].line` 에 정확한 line 번호를 채워
    넣을 수 있도록 hunk 헤더 + 본문 라인을 다음 형식으로 변환한다:

        @@ -10,3 +10,5 @@ context-after-hunk
            10|  context line (hunk 안의 unchanged)
              | -removed line  (LEFT 만 존재 — RIGHT 라인 번호 없음)
            11| +added line 1  (RIGHT, `+` 마커 보존)
            12| +added line 2
            13|  another context

    형식 규칙:
      - hunk 헤더 (`@@ ...`): 그대로 유지 + 줄바꿈
      - context (` `) / 추가 (`+`) 라인: `  NNNNN| ` prefix + 원본 marker + 본문
      - 제거 (`-`) 라인: `       | -본문` (5자 공백 + `|` — 정렬 위해)
      - file 헤더 (`+++`/`---`) / `\\ No newline...` 같은 메타: 통과 (들여쓰기 없이)
      - 빈 patch (None or "") → 빈 문자열 반환

    회귀 방지: `addable_lines_from_patch` 와 같은 카운터 규칙 (`+`/` ` 만 RIGHT 라인
    번호 진행, `-` 는 무시) 을 따라 두 함수가 같은 라인 집합을 produce. 한쪽만 잘못
    수정되면 인라인 게시 (addable) 와 model 이 본 라인 번호 (formatted) 가 어긋남.
    """
    if not patch:
        return ""
    out: list[str] = []
    new_line_no = 0
    for line in patch.splitlines():
        if line.startswith("@@"):
            out.append(line)
            match = _HUNK_HEADER.match(line)
            if match:
                # hunk 시작 직전 위치 — 첫 +/space 라인을 만나면 +1 되어 정확한 시작 번호.
                new_line_no = int(match.group(1)) - 1
            continue
        if line.startswith("+++ ") or line.startswith("--- "):
            # diff 파일 헤더 — addable 계산과 동일 규칙 (codex PR #26 review #5).
            # trailing space 매칭으로 `+++X`/`---X` content 와 구분.
            out.append(line)
            continue
        if line.startswith(("+", " ")):
            new_line_no += 1
            marker = line[:1]  # '+' or ' '
            body = line[1:]
            out.append(f"  {new_line_no:5d}| {marker}{body}")
        elif line.startswith("-"):
            # LEFT-only — RIGHT 에 존재 안 함. line 번호 자리는 공백.
            body = line[1:]
            out.append(f"       | -{body}")
        else:
            # `\ No newline at end of file` 같은 메타 라인 — 통과.
            out.append(line)
    return "\n".join(out)
