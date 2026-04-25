"""DiffBasedResolutionChecker 단위 테스트.

핵심: 본 봇이 이전 push 에서 단 [Critical]/[Major] 라인 코멘트의 대상 라인이 새 push
에서 변경됐으면 부모 코멘트 thread 에 follow-up 대댓글 게시. 변경 안 됐으면 reply 안 함.

검증 발동 조건 (모두 만족):
1. 차단급 finding ([Critical] 또는 [Major])
2. top-level 코멘트 (대댓글 자체는 대상 아님)
3. 본 봇이 아직 follow-up 대댓글을 안 단 코멘트
4. comment.commit_id != pr.head_sha (시간상 후속 push 가 있었음)
5. 라인 본문이 두 SHA 사이에 변경됨
"""
import logging
import subprocess
from pathlib import Path

import pytest

from gemini_review.domain import (
    PostedReviewComment,
    PullRequest,
    RepoRef,
)
from gemini_review.infrastructure.diff_based_resolution_checker import (
    DiffBasedResolutionChecker,
)


def _pr(head_sha: str = "newshahead") -> PullRequest:
    return PullRequest(
        repo=RepoRef("o", "r"),
        number=42,
        title="t",
        body="",
        head_sha=head_sha,
        head_ref="feat",
        base_sha="def",
        base_ref="main",
        clone_url="https://example/r.git",
        changed_files=("a.py",),
        installation_id=7,
        is_draft=False,
    )


def _posted(
    *,
    comment_id: int,
    commit_id: str,
    path: str,
    line: int,
    body: str,
    in_reply_to_id: int | None = None,
    original_commit_id: str | None = None,
    original_line: int | None = None,
) -> PostedReviewComment:
    """Layer E 테스트 helper. `original_*` 미지정 시 `commit_id`/`line` 으로 fallback —
    line shift 가 없는 일반적 시나리오를 모사. shift 있는 케이스는 명시적으로 지정.
    """
    return PostedReviewComment(
        comment_id=comment_id,
        commit_id=commit_id,
        path=path,
        line=line,
        body=body,
        in_reply_to_id=in_reply_to_id,
        original_commit_id=original_commit_id if original_commit_id is not None else commit_id,
        original_line=original_line if original_line is not None else line,
    )


class _FakeGitHub:
    """GitHubClient 의 follow-up 관련 두 메서드만 fake."""

    def __init__(
        self,
        existing: tuple[PostedReviewComment, ...] = (),
        raise_on_list: Exception | None = None,
        raise_on_reply: Exception | None = None,
    ) -> None:
        self._existing = existing
        self._raise_on_list = raise_on_list
        self._raise_on_reply = raise_on_reply
        self.list_call_count = 0
        self.replies: list[tuple[PullRequest, int, str]] = []

    def list_self_review_comments(self, pr: PullRequest) -> tuple[PostedReviewComment, ...]:
        self.list_call_count += 1
        if self._raise_on_list is not None:
            raise self._raise_on_list
        return self._existing

    def reply_to_review_comment(
        self, pr: PullRequest, comment_id: int, body: str
    ) -> None:
        if self._raise_on_reply is not None:
            raise self._raise_on_reply
        self.replies.append((pr, comment_id, body))

    # 다른 메서드는 호출되면 안 됨 — wiring 회귀 방지
    def fetch_pull_request(self, *_a: object, **_k: object) -> object:
        raise AssertionError("fetch_pull_request should not be called by checker")

    def post_review(self, *_a: object, **_k: object) -> None:
        raise AssertionError("post_review should not be called by checker")

    def post_comment(self, *_a: object, **_k: object) -> None:
        raise AssertionError("post_comment should not be called by checker")

    def get_installation_token(self, *_a: object, **_k: object) -> str:
        raise AssertionError("get_installation_token should not be called by checker")


def _patch_git_show(
    monkeypatch: pytest.MonkeyPatch,
    contents: dict[tuple[str, str], str],
) -> None:
    """`git show {sha}:{path}` 응답을 (sha, path) 키 dict 로 시뮬레이션.

    딕셔너리에 키 없으면 returncode=1 (commit unreachable) 시뮬.
    """

    class _FakeCompleted:
        def __init__(self, returncode: int, stdout: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout

    def fake_run(cmd: list[str], **_kwargs: object) -> _FakeCompleted:
        # cmd: ["git", "-C", str(repo_root), "show", f"{sha}:{path}"]
        spec = cmd[-1]  # "{sha}:{path}"
        sha, _, path = spec.partition(":")
        if (sha, path) in contents:
            return _FakeCompleted(0, contents[(sha, path)])
        return _FakeCompleted(1, "")

    monkeypatch.setattr(subprocess, "run", fake_run)


# --- reply 발동 ----------------------------------------------------------------


def test_check_replies_when_blocking_comment_line_changed_between_pushes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """이전 push 에 단 [Major] 라인이 새 push 에서 본문 바뀌면 부모 thread 에 follow-up reply.

    회귀 방지: 사용자 요청 (2026-04) — "라인 코멘트를 일방적으로 다는 것에서 끝나지
    않고 후속 수정사항이 생기면 본인이 단 코멘트에 대댓글로 수정 여부 확인". 이 layer
    의 핵심 동작.

    GitHub 의 코멘트 추적: 코멘트가 처음 만들어진 시점은 `original_commit_id` /
    `original_line`. 후속 push 가 일어나면 GitHub 가 `commit_id`/`line` 을 head 시점
    으로 갱신. Layer E 는 두 anchor (original vs current) 의 본문을 비교.
    """
    prior_sha = "oldshaaa"
    head_sha = "newshahead"
    existing = (
        _posted(
            comment_id=1001,
            commit_id=head_sha,  # GitHub 가 head 로 추적 갱신
            path="a.py",
            line=10,
            body="[Major] 변수명 typo: usrname",
            original_commit_id=prior_sha,  # 코멘트 anchor 시점
            original_line=10,
        ),
    )
    _patch_git_show(monkeypatch, {
        (prior_sha, "a.py"): "\n" * 9 + "x = usrname  # 잘못된 변수명\n",
        (head_sha, "a.py"): "\n" * 9 + "x = username  # 수정됨\n",
    })

    fake = _FakeGitHub(existing=existing)
    DiffBasedResolutionChecker(fake).check_resolutions(_pr(head_sha=head_sha), tmp_path)

    assert len(fake.replies) == 1, "라인 변경됨 → follow-up reply 1건"
    pr_arg, cid, body = fake.replies[0]
    assert pr_arg.head_sha == head_sha
    assert cid == 1001
    assert "라인이 변경되었습니다" in body
    assert "oldshaa" in body  # prior sha 7-char prefix
    assert "newshah" in body  # head sha 7-char prefix
    assert "x = usrname" in body, "이전 라인 본문이 reply 에 인용돼야"
    assert "x = username" in body, "현재 라인 본문이 reply 에 인용돼야"
    assert "확인 부탁드립니다" in body, "메인테이너가 직접 판단하도록 안내 톤"


def test_check_replies_to_critical_comment_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[Critical] 도 follow-up 대상 — 두 차단급 모두 처리."""
    existing = (
        _posted(
            comment_id=1002,
            commit_id="newshahead",  # GitHub-tracked current
            path="b.py",
            line=5,
            body="[Critical] 보안: 직접 SQL 조립",
            original_commit_id="oldshaaa",  # anchor 시점
            original_line=5,
        ),
    )
    _patch_git_show(monkeypatch, {
        ("oldshaaa", "b.py"): "\n" * 4 + "raw_sql\n",
        ("newshahead", "b.py"): "\n" * 4 + "params_sql\n",
    })

    fake = _FakeGitHub(existing=existing)
    DiffBasedResolutionChecker(fake).check_resolutions(_pr(), tmp_path)

    assert len(fake.replies) == 1


# --- reply 안 함 (false positive 방지) -----------------------------------------


def test_check_uses_original_anchor_so_line_shift_alone_does_not_trigger_reply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """라인 shift (위쪽에 새 코드 추가) 만 일어나고 본문 같으면 reply 안 함.

    GitHub 가 코멘트 line 을 +N 으로 추적 갱신해도 본문 동일이면 메인테이너 처리 신호 X.

    회귀 방지 (gemini PR #28 review #1): 이전 구현은 `comment.commit_id` / `comment.line`
    을 prior 측 anchor 로 사용. GitHub 가 line shift 추적으로 두 값을 head 시점으로
    갱신하면 prior 측이 곧 head 측이 되어 항상 같은 본문 → 영원히 reply 안 함 (Layer E
    무력화). 이제는 `original_commit_id` / `original_line` (anchor 보존됨) 을 prior 측
    으로, `commit_id` / `line` (GitHub 추적 후 위치) 을 current 측으로 사용 → 라인이
    옮겨졌을 뿐 본문 같음을 정확히 식별.
    """
    prior_sha = "oldshaaa"
    head_sha = "newshahead"
    existing = (
        _posted(
            comment_id=4001,
            # GitHub 가 head 로 commit_id 갱신 + 위쪽에 새 라인 추가로 line 도 +5 갱신
            commit_id=head_sha,
            line=15,
            path="a.py",
            body="[Major] 잠재 버그",
            # 코멘트 anchor 시점의 SHA / line 은 보존됨
            original_commit_id=prior_sha,
            original_line=10,
        ),
    )
    # 같은 본문이 prior 시점엔 line 10 에, head 시점엔 (위쪽 코드 추가로) line 15 에 위치.
    _patch_git_show(monkeypatch, {
        (prior_sha, "a.py"): "\n" * 9 + "x = 1\n",   # line 10
        (head_sha, "a.py"): "\n" * 14 + "x = 1\n",  # line 15 (같은 본문, 위치만 이동)
    })

    fake = _FakeGitHub(existing=existing)
    DiffBasedResolutionChecker(fake).check_resolutions(_pr(head_sha=head_sha), tmp_path)

    assert fake.replies == [], (
        "라인이 옮겨졌을 뿐 본문은 같음 → 메인테이너 처리 신호 X → reply 안 함"
    )


def test_check_skips_when_line_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """라인 본문이 두 SHA 사이에 동일하면 reply 안 함 — 메인테이너 처리 신호 X."""
    existing = (
        _posted(
            comment_id=1003,
            commit_id="oldshaaa",
            path="a.py",
            line=10,
            body="[Major] 잠재 버그",
        ),
    )
    _patch_git_show(monkeypatch, {
        ("oldshaaa", "a.py"): "\n" * 9 + "same line\n",
        ("newshahead", "a.py"): "\n" * 9 + "same line\n",
    })

    fake = _FakeGitHub(existing=existing)
    DiffBasedResolutionChecker(fake).check_resolutions(_pr(), tmp_path)

    assert fake.replies == [], "라인 본문 동일 → reply 안 함"


def test_check_skips_minor_and_suggestion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """비-블로킹 finding ([Minor]/[Suggestion]) 은 follow-up 대상 X — noise 회피.

    이유: Minor/Suggestion 은 메인테이너가 무시해도 정상이라 "수정됐나?" reply 가
    노이즈가 됨. 차단급만 follow-up 가치 있음.
    """
    existing = (
        _posted(
            comment_id=1004,
            commit_id="oldshaaa",
            path="a.py",
            line=10,
            body="[Minor] 변수명 권고",
        ),
        _posted(
            comment_id=1005,
            commit_id="oldshaaa",
            path="a.py",
            line=20,
            body="[Suggestion] 리팩터 제안",
        ),
    )
    # git show 설정해도 reply 발동 안 해야
    _patch_git_show(monkeypatch, {
        ("oldshaaa", "a.py"): "\n" * 19 + "old\n",
        ("newshahead", "a.py"): "\n" * 19 + "new\n",
    })

    fake = _FakeGitHub(existing=existing)
    DiffBasedResolutionChecker(fake).check_resolutions(_pr(), tmp_path)

    assert fake.replies == [], "비-블로킹은 reply 안 함"


def test_check_skips_when_commit_id_equals_head_sha(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """comment.commit_id 가 head_sha 와 같으면 그 사이 push 가 없었다는 뜻 → skip.

    이번 push 에서 봇이 직접 단 코멘트를 자기 자신에게 reply 하는 회귀 방지.
    """
    head_sha = "newshahead"
    existing = (
        _posted(
            comment_id=1006,
            commit_id=head_sha,  # 같은 SHA
            path="a.py",
            line=10,
            body="[Major] 방금 단 코멘트",
        ),
    )

    fake = _FakeGitHub(existing=existing)
    DiffBasedResolutionChecker(fake).check_resolutions(_pr(head_sha=head_sha), tmp_path)

    assert fake.replies == [], "같은 SHA → 변경 가능성 0 → reply 안 함"


def test_check_skips_when_already_replied_to(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """본 봇이 이미 follow-up 대댓글 단 코멘트는 또 reply 안 함 — 중복 회피.

    회귀 방지: 매 push 마다 같은 (오래된 commit_id ↔ 새 head_sha) 비교가 발동하면
    매번 새 reply 가 달림. in_reply_to_id 가 set 된 코멘트들에서 부모 id 를 추적해
    "이미 reply 한 부모" 셋을 만들고 거기 들어 있으면 skip.
    """
    existing = (
        # 부모 코멘트: 라인 10, 차단급
        _posted(
            comment_id=1007,
            commit_id="oldshaaa",
            path="a.py",
            line=10,
            body="[Major] 잠재 버그",
        ),
        # 본 봇이 이미 단 follow-up reply (in_reply_to_id=1007)
        _posted(
            comment_id=1008,
            commit_id="midshaaa",  # reply 시점 sha
            path="a.py",
            line=10,
            body="📌 라인이 변경되었습니다 ...",
            in_reply_to_id=1007,
        ),
    )
    _patch_git_show(monkeypatch, {
        ("oldshaaa", "a.py"): "\n" * 9 + "old\n",
        ("newshahead", "a.py"): "\n" * 9 + "new\n",
    })

    fake = _FakeGitHub(existing=existing)
    DiffBasedResolutionChecker(fake).check_resolutions(_pr(), tmp_path)

    assert fake.replies == [], "이미 reply 한 부모 → skip (중복 회피)"


def test_check_skips_when_git_show_fails_for_either_sha(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """git show 가 어느 한 SHA 라도 못 읽으면 reply 안 함 (graceful skip).

    회귀 방지: force-push 후 prior commit 이 unreachable 한 케이스, 또는 파일이 그
    commit 에 없던 케이스. false reply 보다 reply 안 하는 게 안전.
    """
    existing = (
        _posted(
            comment_id=1009,
            commit_id="lostsha",
            path="a.py",
            line=10,
            body="[Major] 잠재 버그",
        ),
    )
    # head SHA 만 있고 prior 는 없음 (force-push 후 unreachable 시뮬)
    _patch_git_show(monkeypatch, {
        ("newshahead", "a.py"): "\n" * 9 + "current\n",
    })

    fake = _FakeGitHub(existing=existing)
    DiffBasedResolutionChecker(fake).check_resolutions(_pr(), tmp_path)

    assert fake.replies == [], "prior commit unreachable → skip"


def test_check_skips_replies_themselves_from_top_level_iteration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """history 안에 본 봇 자신의 reply 가 섞여 있어도 그것을 top-level 처럼 처리하지 않음."""
    existing = (
        # 한 부모 코멘트
        _posted(
            comment_id=1010,
            commit_id="newshahead",  # GitHub-tracked current
            path="a.py",
            line=10,
            body="[Major] 부모",
            original_commit_id="oldshaaa",
            original_line=10,
        ),
        # 다른 부모 코멘트의 본 봇 reply (= top-level 아님). 이 reply 자체에 또 reply
        # 시도하면 안 됨. (in_reply_to_id 가 set 돼 있어 top-level 검사에서 제외돼야)
        _posted(
            comment_id=1011,
            commit_id="midsha",
            path="b.py",
            line=99,
            body="📌 ...",
            in_reply_to_id=999,  # 다른 부모
        ),
    )
    _patch_git_show(monkeypatch, {
        ("oldshaaa", "a.py"): "\n" * 9 + "old\n",
        ("newshahead", "a.py"): "\n" * 9 + "new\n",
        # b.py 데이터 안 줘도 reply 안 발동돼야 함
    })

    fake = _FakeGitHub(existing=existing)
    DiffBasedResolutionChecker(fake).check_resolutions(_pr(), tmp_path)

    # 부모 1010 만 처리되어 reply 1건
    assert len(fake.replies) == 1
    assert fake.replies[0][1] == 1010, "1010 (부모) 에만 reply, 1011 (자체 reply) 은 무시"


# --- Graceful degrade ---------------------------------------------------------


def test_check_graceful_degrade_on_list_api_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """list_self_review_comments 실패 → WARN 로그만 + 조용히 종료. 리뷰 게시 흐름엔 영향 X.

    회귀 방지: Layer E 는 방어 layer 이지 차단 layer 아님. 이 layer 의 어떤 실패도
    이미 끝난 post_review 흐름에 영향 주면 안 됨.
    """
    fake = _FakeGitHub(raise_on_list=RuntimeError("rate limit"))

    with caplog.at_level(logging.WARNING):
        DiffBasedResolutionChecker(fake).check_resolutions(_pr(), tmp_path)

    assert fake.replies == [], "list 실패 → 어떤 reply 도 안 시도"
    assert any(
        "list_self_review_comments failed" in r.getMessage() for r in caplog.records
    ), "운영 진단을 위해 WARN 1건 이상"


def test_check_graceful_degrade_on_per_reply_failure_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """한 코멘트의 reply 게시 실패가 다음 코멘트 처리를 막지 않아야.

    회귀 방지: per-comment 실패가 batch 전체를 죽이면 첫 코멘트만 일시 장애 나도
    뒤의 N-1 개 코멘트가 모두 누락. 각 reply 는 독립 처리.
    """
    existing = (
        _posted(
            comment_id=2001,
            commit_id="newshahead",
            path="a.py",
            line=10,
            body="[Major] 첫 코멘트",
            original_commit_id="oldshaaa",
            original_line=10,
        ),
        _posted(
            comment_id=2002,
            commit_id="newshahead",
            path="b.py",
            line=20,
            body="[Major] 두번째 코멘트",
            original_commit_id="oldshaaa",
            original_line=20,
        ),
    )
    _patch_git_show(monkeypatch, {
        ("oldshaaa", "a.py"): "\n" * 9 + "old1\n",
        ("newshahead", "a.py"): "\n" * 9 + "new1\n",
        ("oldshaaa", "b.py"): "\n" * 19 + "old2\n",
        ("newshahead", "b.py"): "\n" * 19 + "new2\n",
    })

    fake = _FakeGitHub(existing=existing, raise_on_reply=RuntimeError("API 422"))

    with caplog.at_level(logging.WARNING):
        DiffBasedResolutionChecker(fake).check_resolutions(_pr(), tmp_path)

    # raise_on_reply 가 영구적이라 두 시도 모두 실패하지만, 두 번째도 시도 자체는
    # 일어나야 (continue 동작 lock). 카운트는 fake 에서 잡지 않으니 WARN 로그 2건
    # 이 뜨는지 확인.
    warns = [
        r for r in caplog.records if "reply_to_review_comment failed" in r.getMessage()
    ]
    assert len(warns) == 2, f"두 코멘트 모두 reply 시도되어야 (각 실패 시 WARN). 실제 {len(warns)}"


# --- Empty / no-op cases ------------------------------------------------------


def test_check_safely_handles_undecodable_bytes_in_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """git show 응답에 UTF-8 디코딩 불가 바이트가 있어도 Layer E 가 터지면 안 됨.

    회귀 방지 (gemini PR #28 review #2): subprocess `text=True` + 명시 encoding 없음 →
    바이너리/non-UTF8 파일에서 UnicodeDecodeError (ValueError 하위). 우리 except 블록은
    OSError/SubprocessError 만 잡아 전체 후속 검사가 비정상 종료. 이제는
    `errors="replace"` 로 안전 디코딩.

    이 테스트는 git show 가 invalid 바이트를 stdout 으로 돌려주는 시나리오를 모사 — 실
    환경에선 subprocess 가 디코딩하지만 fake_run 에서 직접 replace 결과를 시뮬.
    """

    class _FakeCompleted:
        def __init__(self, returncode: int, stdout: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout

    def fake_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
        # `errors="replace"` 가 정확히 전달됐는지 확인 — 누락되면 이 테스트는 통과
        # 하더라도 production 환경에서 회귀 가능
        assert kwargs.get("encoding") == "utf-8", "encoding=utf-8 명시 필요"
        assert kwargs.get("errors") == "replace", "errors=replace 명시 필요"
        spec = cmd[-1]
        sha, _, _ = spec.partition(":")
        # 양쪽 sha 모두 invalid byte 가 replace 된 결과 시뮬 (실 환경에서 errors=replace
        # 가 � 로 치환). 두 sha 의 같은 라인 모두 동일 치환 결과 → no diff → no reply.
        if sha == "oldshaaa":
            return _FakeCompleted(0, "\n" * 9 + "x = ��\n")
        return _FakeCompleted(0, "\n" * 9 + "x = ��\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    existing = (
        _posted(
            comment_id=5001,
            commit_id="newshahead",
            path="bin.dat",
            line=10,
            body="[Major] 바이너리 파일 지적",
            original_commit_id="oldshaaa",
            original_line=10,
        ),
    )

    fake = _FakeGitHub(existing=existing)
    # 예외 없이 정상 흐름으로 끝나야
    DiffBasedResolutionChecker(fake).check_resolutions(_pr(), tmp_path)
    assert fake.replies == [], "동일 치환 결과 비교 → 변경 없음 → reply 안 함 (그리고 예외 없음)"


def test_check_skips_when_only_indentation_or_trailing_space_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """들여쓰기 / trailing space 만 바뀐 라인은 reply 안 함 (formatter 자동 정리 noise 회피).

    회귀 방지 (gemini PR #28 review #5): 정확 일치 비교는 black/prettier 같은 formatter
    가 들여쓰기/공백을 자동 정리한 경우에도 reply 발동 → noise. strip 후 본문이 같으면
    의미 있는 변경 아니므로 reply 안 함.
    """
    existing = (
        _posted(
            comment_id=8001,
            commit_id="newshahead",
            path="a.py",
            line=10,
            body="[Major] 잠재 버그",
            original_commit_id="oldshaaa",
            original_line=10,
        ),
    )
    # 같은 본문 — formatter 가 들여쓰기 4칸 → 2칸으로 변경 + trailing space 정리.
    # strip 결과는 동일.
    _patch_git_show(monkeypatch, {
        ("oldshaaa", "a.py"): "\n" * 9 + "    x = 1   \n",   # 4-space indent + trailing
        ("newshahead", "a.py"): "\n" * 9 + "  x = 1\n",       # 2-space indent + cleaned
    })

    fake = _FakeGitHub(existing=existing)
    DiffBasedResolutionChecker(fake).check_resolutions(_pr(), tmp_path)

    assert fake.replies == [], (
        "들여쓰기/trailing space 만 바뀐 라인은 reply 안 함 (formatter 자동 정리 noise 회피)"
    )


def test_check_reply_body_sha_matches_actually_read_sha_not_pr_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """reply 본문/로그에 표시되는 head SHA = head_line 을 실제 읽은 SHA (= comment.commit_id).

    회귀 방지 (coderabbitai PR #28 review #4 + gemini round 1 line 132): GitHub 가 아직
    comment.commit_id 를 pr.head_sha 로 추적 갱신하지 못한 순간 "표시 SHA" 와 "비교 SHA"
    가 어긋나 메인테이너가 잘못된 commit 을 확인하는 혼란. reply 본문에 표시되는 head
    SHA 는 반드시 head_line 을 실제 읽은 SHA (= comment.commit_id) 와 같아야.
    """
    # GitHub 가 추적 갱신을 못한 시뮬: comment.commit_id 와 pr.head_sha 가 다름
    pr_head = "newshahead"
    comment_current_sha = "midshaaaa"  # GitHub 가 여기까지만 추적 갱신
    existing = (
        _posted(
            comment_id=7001,
            commit_id=comment_current_sha,  # GitHub-tracked < pr.head_sha
            path="a.py",
            line=10,
            body="[Major] 잠재 버그",
            original_commit_id="oldshaaa",
            original_line=10,
        ),
    )
    _patch_git_show(monkeypatch, {
        ("oldshaaa", "a.py"): "\n" * 9 + "old\n",
        (comment_current_sha, "a.py"): "\n" * 9 + "new\n",
    })

    fake = _FakeGitHub(existing=existing)
    DiffBasedResolutionChecker(fake).check_resolutions(_pr(head_sha=pr_head), tmp_path)

    assert len(fake.replies) == 1
    body = fake.replies[0][2]
    # 본문에 보여줘야 하는 head SHA = 실제 head_line 읽은 SHA (= comment.commit_id)
    assert comment_current_sha[:7] in body, (
        f"reply 본문의 head SHA 는 head_line 을 실제 읽은 SHA ({comment_current_sha[:7]}) "
        f"와 일치해야. 실제 본문: {body!r}"
    )
    # pr.head_sha (모델이 새 리뷰 게시한 SHA) 는 본문에 등장하면 안 됨 — 두 값이
    # 같을 수도 있지만 이 테스트의 시나리오는 다른 값 (GitHub 추적 lag) 을 모사
    assert pr_head[:7] not in body, (
        f"reply 본문에 pr.head_sha ({pr_head[:7]}) 가 들어가면 비교 SHA 와 어긋남"
    )


def test_check_renders_line_content_in_code_block_safe_to_backticks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """라인 본문에 backtick 이 포함돼도 markdown 깨지지 않게 4-space indent 코드 블록으로 렌더.

    회귀 방지 (gemini PR #28 review #4): inline backtick (`` `{line}` ``) 로 감싸면
    line 에 backtick 이 있을 때 마크다운 깨짐. 4-space indent code block 은 fence 와
    무관해 어떤 backtick 개수의 본문도 그대로 표시.
    """
    existing = (
        _posted(
            comment_id=6001,
            commit_id="newshahead",
            path="README.md",
            line=10,
            body="[Major] 마크다운 docstring 지적",
            original_commit_id="oldshaaa",
            original_line=10,
        ),
    )
    # 양쪽 모두 backtick 포함된 라인 (마크다운 / docstring 흔한 케이스)
    _patch_git_show(monkeypatch, {
        ("oldshaaa", "README.md"): "\n" * 9 + "use `legacy_api()`\n",
        ("newshahead", "README.md"): "\n" * 9 + "use `new_api()` instead\n",
    })

    fake = _FakeGitHub(existing=existing)
    DiffBasedResolutionChecker(fake).check_resolutions(_pr(), tmp_path)

    assert len(fake.replies) == 1
    body = fake.replies[0][2]
    # inline backtick 으로 라인을 감싸지 않아야 — 4-space indent 형태로 렌더
    assert "    use `legacy_api()`" in body, "이전 라인은 4-space indent 로 렌더"
    assert "    use `new_api()` instead" in body, "현재 라인은 4-space indent 로 렌더"
    # `**이전:** \`use \`legacy_api()\`\`` 같은 inline backtick wrapping 이 없어야
    assert "**이전:** `use" not in body, "inline backtick wrapping 회귀 X"
    assert "**현재:** `use" not in body


def test_check_no_op_when_no_prior_comments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """history 비었으면 git 호출도 안 하고 즉시 종료."""
    fake = _FakeGitHub(existing=())
    # subprocess.run 을 호출하면 안 됨 — 호출되면 unmocked 라 진짜 git 실행 시도

    DiffBasedResolutionChecker(fake).check_resolutions(_pr(), tmp_path)

    assert fake.list_call_count == 1, "API 호출은 1회 (어떤 history 인지 봐야)"
    assert fake.replies == []
