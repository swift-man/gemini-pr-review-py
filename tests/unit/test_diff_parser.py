"""diff_parser.addable_lines_from_patch 의 엣지 케이스 고정.

이 함수는 GitHub Reviews API 의 "comments[].line 은 diff 안 라인이어야 한다" 룰을
서버 호출 없이 재현한다 — 잘못 계산되면 422 가 다시 출현하므로 회귀 방지가 중요.
"""

from gemini_review.infrastructure.diff_parser import addable_lines_from_patch


def test_added_lines_only() -> None:
    """순수 추가 hunk — 추가된 라인만 RIGHT 사이드에 들어간다."""
    patch = (
        "@@ -10,0 +11,3 @@\n"
        "+added line 11\n"
        "+added line 12\n"
        "+added line 13\n"
    )
    assert addable_lines_from_patch(patch) == {11, 12, 13}


def test_added_lines_with_context() -> None:
    """추가 + context 혼합 — context 라인도 인라인 코멘트 가능 (GitHub 룰)."""
    patch = (
        "@@ -10,3 +10,4 @@\n"
        " context 10\n"
        " context 11\n"
        "+added 12\n"
        " context 13\n"
    )
    assert addable_lines_from_patch(patch) == {10, 11, 12, 13}


def test_removed_lines_do_not_advance_new_counter() -> None:
    """제거(`-`) 라인은 RIGHT 사이드에 없어 카운터를 옮기지 않는다.

    회귀 방지: 만약 `-` 라인에서 new_line_no 를 증가시키면 이후 모든 추가 라인의
    인덱스가 1씩 밀려 422 가 발생한다.
    """
    patch = (
        "@@ -10,4 +10,3 @@\n"
        " context 10\n"
        "-removed (was line 11 in old)\n"
        "+added 11 in new\n"
        " context 12\n"
    )
    # 정답: {10, 11, 12} — 제거된 라인이 카운터를 안 옮김
    assert addable_lines_from_patch(patch) == {10, 11, 12}


def test_multiple_hunks() -> None:
    """여러 hunk 가 한 patch 에 있는 경우 — 각 hunk 의 헤더로 카운터가 리셋된다."""
    patch = (
        "@@ -10,2 +10,3 @@\n"
        " ctx\n"
        "+a\n"
        " ctx\n"
        "@@ -100,1 +101,2 @@\n"
        " ctx\n"
        "+b\n"
    )
    assert addable_lines_from_patch(patch) == {10, 11, 12, 101, 102}


def test_diff_file_headers_are_ignored() -> None:
    """`+++ b/path` / `--- a/path` 헤더는 추가 라인이 아니다."""
    patch = (
        "--- a/src/x.py\n"
        "+++ b/src/x.py\n"
        "@@ -1,1 +1,2 @@\n"
        " ctx\n"
        "+real add\n"
    )
    assert addable_lines_from_patch(patch) == {1, 2}


def test_no_newline_marker_is_ignored() -> None:
    """`\\ No newline at end of file` 메타 라인은 카운터에 영향 없음."""
    patch = (
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
        "\\ No newline at end of file\n"
    )
    assert addable_lines_from_patch(patch) == {1}


def test_empty_or_none_patch_returns_empty_set() -> None:
    """binary 파일 / 삭제 / GitHub truncate 케이스 — patch 가 비거나 None."""
    assert addable_lines_from_patch(None) == set()
    assert addable_lines_from_patch("") == set()


def test_single_line_addition_without_count_in_header() -> None:
    """hunk 헤더의 count 가 생략 가능: `@@ -10 +10 @@` (1줄일 때)."""
    patch = (
        "@@ -10 +10 @@\n"
        "-old\n"
        "+new\n"
    )
    assert addable_lines_from_patch(patch) == {10}


def test_large_hunk_starting_at_high_line() -> None:
    """파일 후반부의 hunk — 카운터가 정확히 새 시작 위치에서 출발."""
    patch = (
        "@@ -1000,3 +1500,3 @@\n"
        " a\n"
        "-b\n"
        "+B\n"
        " c\n"
    )
    assert addable_lines_from_patch(patch) == {1500, 1501, 1502}


# --- format_patch_with_line_numbers (diff fallback 입력 포맷) ---------------

from gemini_review.infrastructure.diff_parser import (  # noqa: E402
    format_patch_with_line_numbers,
)


def test_format_returns_empty_for_none_or_empty_patch() -> None:
    assert format_patch_with_line_numbers(None) == ""
    assert format_patch_with_line_numbers("") == ""


def test_format_annotates_added_and_context_lines_with_right_numbers() -> None:
    """`+` / ` ` 라인 모두 RIGHT 라인 번호 부여, `-` 라인은 번호 없음.

    회귀 방지: 모델이 `comments[].line` 채울 때 이 prefix 의 NNNNN 을 직접 읽어 쓴다.
    잘못 annotate 되면 모든 인라인 코멘트가 잘못된 라인에 붙는다 (사용자 큰 혼란).
    """
    patch = (
        "@@ -10,3 +10,5 @@\n"
        " context A\n"
        "-removed B\n"
        "+added C\n"
        "+added D\n"
        " context E\n"
    )
    out = format_patch_with_line_numbers(patch)

    assert "@@ -10,3 +10,5 @@" in out, "hunk 헤더는 그대로 통과"
    assert "     10|  context A" in out, "context: 시작 라인 10 + ' ' marker"
    assert "       | -removed B" in out, "remove: RIGHT 라인 없음 (공백 prefix)"
    assert "     11| +added C" in out, "added: RIGHT 11"
    assert "     12| +added D" in out, "added: RIGHT 12"
    assert "     13|  context E" in out, "context after adds: RIGHT 13"


def test_format_keeps_addable_lines_consistent_with_addable_lines_from_patch() -> None:
    """format 의 RIGHT 번호 집합 = addable_lines_from_patch 결과.

    회귀 방지: 두 함수가 같은 카운터 규칙을 따라야 함. 한쪽만 수정되면 모델이 본
    라인 번호와 게시 가능 라인 사이가 어긋나 422 가 다시 출현 (또는 surface 처리).
    """
    import re

    patch = (
        "@@ -1,3 +1,4 @@\n"
        " a\n"
        "-b\n"
        "+B\n"
        "+B2\n"
        " c\n"
        "@@ -100,2 +200,3 @@\n"
        " x\n"
        "+Y\n"
        " z\n"
    )
    formatted = format_patch_with_line_numbers(patch)

    # `  NNNNN| ` prefix 에서 번호를 뽑아낸다 (` ` / `+` 라인 모두)
    annotated_right = {
        int(m.group(1)) for m in re.finditer(r"^\s+(\d+)\| [+ ]", formatted, re.M)
    }
    assert annotated_right == addable_lines_from_patch(patch)


def test_format_preserves_no_newline_meta_line() -> None:
    """`\\ No newline at end of file` 같은 메타 라인은 통과."""
    patch = (
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
        "\\ No newline at end of file\n"
    )
    out = format_patch_with_line_numbers(patch)

    assert "\\ No newline at end of file" in out


# --- file header vs content (codex PR #26 review #5) ------------------------


def test_addable_does_not_skip_content_starting_with_double_plus() -> None:
    """JS `++counter` 추가 라인 (diff: `+++counter`) 은 file header 가 아니라 RIGHT addable.

    회귀 방지 (codex PR #26 review #5): 이전엔 `startswith("+++")` 만 검사해 `+++counter`
    같은 content 도 file header 로 오인 → RIGHT 카운터 안 증가 → 후속 addable 가 모두
    -1 어긋나 422 회귀. 이제는 `+++ ` (trailing space) 만 헤더로 인정.
    """
    patch = (
        "@@ -10,2 +10,4 @@\n"
        " context\n"
        "+normal\n"
        "+++counter\n"  # JS pre-increment 추가 — `+` marker + `++counter` content
        "+after\n"
    )
    # 모든 `+` / context 라인이 RIGHT 에 들어가야: 10 (context), 11 (+normal),
    # 12 (++counter), 13 (+after)
    assert addable_lines_from_patch(patch) == {10, 11, 12, 13}


def test_addable_does_not_skip_content_starting_with_double_minus() -> None:
    """`--counter` 제거 라인 (diff: `---counter`) 은 file header 가 아니지만 RIGHT 미진행.

    `-` 본문이라 RIGHT 카운터엔 영향 없음 — 잘못 처리해도 addable 결과는 같지만,
    format 결과는 어긋남 (다음 테스트로 lock).
    """
    patch = (
        "@@ -10,3 +10,2 @@\n"
        " context\n"
        "---counter\n"  # JS pre-decrement 제거 — `-` marker + `--counter` content
        " after\n"
    )
    # context (10) + after (11) — `---counter` 는 RIGHT 에 없음
    assert addable_lines_from_patch(patch) == {10, 11}


def test_format_annotates_content_starting_with_double_plus() -> None:
    """`+++counter` content 가 RIGHT 라인 번호로 annotate 되어야.

    회귀 방지 (codex PR #26 review #5): 이전엔 헤더로 오인해 `+++counter` 가 line 번호
    없이 그대로 통과 + 후속 라인 번호가 모두 -1 어긋남. format 출력은 모델 prompt 입력
    이라 잘못된 번호로 인라인 코멘트가 만들어지면 422 게시 실패 또는 surface 처리.
    """
    patch = (
        "@@ -10,2 +10,4 @@\n"
        " context\n"
        "+normal\n"
        "+++counter\n"
        "+after\n"
    )
    out = format_patch_with_line_numbers(patch)

    assert "     12| +++counter" in out, "++counter 추가 라인은 RIGHT 12 번"
    assert "     13| +after" in out, "그 다음 +after 는 RIGHT 13 번 (오프셋 안 어긋남)"


def test_format_treats_real_file_header_with_trailing_space_as_header() -> None:
    """`+++ b/path` / `--- a/path` (trailing space + path) 는 그대로 file header 로 처리.

    회귀 방지: trailing space 검사 추가 후에도 정상 file header 는 그대로 통과해야 함.
    GitHub `/files` patch 는 보통 `@@` 부터 시작이지만, raw git diff 에는 헤더가 들어옴.
    """
    patch = (
        "--- a/file.py\n"
        "+++ b/file.py\n"
        "@@ -1,1 +1,2 @@\n"
        " a\n"
        "+B\n"
    )
    out = format_patch_with_line_numbers(patch)

    # 파일 헤더는 line 번호 prefix 없이 그대로 통과
    assert "--- a/file.py" in out
    assert "+++ b/file.py" in out
    assert "      1|  a" in out
    assert "      2| +B" in out
