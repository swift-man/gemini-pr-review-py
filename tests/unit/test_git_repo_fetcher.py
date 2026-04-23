"""GitRepoFetcher 의 fetch ref 라우팅 단위 테스트.

핵심: PullRequest.fetch_ref 가 비어있으면 head_sha 로 fallback (역호환), 비어있지 않으면
그 ref 로 fetch + FETCH_HEAD 로 checkout. fork 가 삭제된 PR 에서 base.repo 의
`refs/pull/{n}/head` 로 PR 스냅샷을 받는 경로의 회귀 방지 (codex PR #21 review #1).
"""
import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest

from gemini_review.domain import PullRequest, RepoRef
from gemini_review.infrastructure.git_repo_fetcher import (
    GitRepoFetcher,
    _mask_auth_in_arg,
)


def _make_pr(*, fetch_ref: str = "", clone_url: str = "https://example/x.git") -> PullRequest:
    return PullRequest(
        repo=RepoRef("o", "r"),
        number=42,
        title="t",
        body="",
        head_sha="abc123",
        head_ref="feat",
        base_sha="def456",
        base_ref="main",
        clone_url=clone_url,
        changed_files=("a.py",),
        installation_id=7,
        is_draft=False,
        fetch_ref=fetch_ref,
    )


def _record_subprocess_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> list[list[str]]:
    """subprocess.run 을 가로채 호출된 git 명령 시퀀스를 캡처한다.

    .git 디렉터리는 만들지 않아 clone 경로로 들어가게 하고, 모든 git 호출은 성공으로 흉내.
    """
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_checkout_uses_head_sha_when_fetch_ref_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """기본 (정상) 케이스: fetch_ref 비어있으면 head_sha 로 fetch + checkout.

    회귀 방지: 기존 (PR #21 이전) 호출부와의 호환성. fetch_ref 가 빈 문자열이라도
    `effective_fetch_ref()` 가 head_sha 로 자연 fallback 해야 한다.
    """
    calls = _record_subprocess_calls(monkeypatch, tmp_path)
    fetcher = GitRepoFetcher(cache_dir=tmp_path)

    fetcher.checkout(_make_pr(fetch_ref=""), installation_token="tkn")

    fetch_cmds = [c for c in calls if "fetch" in c and "--depth" in c]
    checkout_cmds = [c for c in calls if "checkout" in c and "--force" in c]
    assert len(fetch_cmds) == 1 and fetch_cmds[0][-1] == "abc123", (
        "fetch_ref 비어있으면 head_sha 로 fetch 해야"
    )
    assert len(checkout_cmds) == 1 and checkout_cmds[0][-1] == "abc123", (
        "fetch_ref 비어있으면 head_sha 로 checkout 해야"
    )


def test_checkout_uses_pr_ref_when_fetch_ref_set_to_pull_ref(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """fork 삭제 fallback 시나리오: fetch_ref 가 `refs/pull/{n}/head` 로 세팅됐을 때.

    회귀 방지 (codex PR #21 review #1): clone_url 만 base 로 바꾸고 fetch 는 여전히
    head_sha 로 시도하면 base 저장소엔 그 SHA 가 없어 실패. fetch_ref 를 PR ref 로
    세팅한 PullRequest 가 들어오면 GitRepoFetcher 가 그걸 사용해 base 의 `refs/pull/`
    로 받아야 한다. 결과 SHA 는 FETCH_HEAD 에 들어가므로 checkout 도 거기로.
    """
    calls = _record_subprocess_calls(monkeypatch, tmp_path)
    fetcher = GitRepoFetcher(cache_dir=tmp_path)

    pr = _make_pr(fetch_ref="refs/pull/42/head", clone_url="https://base/x.git")
    fetcher.checkout(pr, installation_token="tkn")

    fetch_cmds = [c for c in calls if "fetch" in c and "--depth" in c]
    checkout_cmds = [c for c in calls if "checkout" in c and "--force" in c]
    assert len(fetch_cmds) == 1
    # fetch ref 는 PR ref
    assert fetch_cmds[0][-1] == "refs/pull/42/head", (
        "fetch_ref 로 PR ref 가 명시되면 그대로 git fetch 인자로 전달돼야"
    )
    # checkout 은 FETCH_HEAD (PR ref fetch 결과 SHA)
    assert len(checkout_cmds) == 1
    assert checkout_cmds[0][-1] == "FETCH_HEAD", (
        "PR ref fetch 결과는 FETCH_HEAD 에 있으므로 checkout 도 거기로 해야 한다"
    )


def test_effective_fetch_ref_falls_back_to_head_sha_when_empty() -> None:
    """도메인 헬퍼 회귀: `fetch_ref` 가 빈 문자열이면 `head_sha` 를 반환해야 한다.

    이전 호출부 (PR #21 이전 PullRequest 생성 코드) 와의 호환성. 빈 값을 명시적
    "head_sha 사용" 신호로 해석.
    """
    pr = _make_pr(fetch_ref="")
    assert pr.effective_fetch_ref() == "abc123"


def test_effective_fetch_ref_returns_explicit_value_when_set() -> None:
    """명시적 fetch_ref 가 있으면 그대로 반환 (head_sha 무시)."""
    pr = _make_pr(fetch_ref="refs/pull/42/head")
    assert pr.effective_fetch_ref() == "refs/pull/42/head"


def test_checkout_restores_origin_url_even_when_fetch_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """fetch 실패 후에도 토큰 주입된 origin URL 이 .git/config 에 남으면 안 된다.

    회귀 방지 (codex PR #21 review #2 — **보안 회귀**): 이번 PR 이 삭제된 fork PR 을
    실제 git fetch 경로로 보내면서 fetch 실패 가능성이 높아졌다. 이전 코드는 set-url
    복구를 성공 경로 끝에만 두었기에 실패 시 토큰이 디스크에 잔류했다. try/finally 로
    감싸 어떤 경로로 빠져나가든 복구가 항상 호출돼야 한다.

    시뮬레이션: clone 은 성공시키고 (.git 디렉터리 생성), 그 다음 set-url (token 주입)
    까지 성공시킨 뒤 fetch 단계에서 실패. RuntimeError 가 전파되더라도 finally 블록에서
    `remote set-url origin <원래 URL>` 호출이 일어나야 한다.
    """
    # repo_path 를 미리 만들어 .git 존재 → set-url 경로로 가게 함
    repo_path = tmp_path / "o" / "r"
    (repo_path / ".git").mkdir(parents=True)

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        # fetch 호출에서 실패 — 토큰이 이미 set-url 로 주입된 상태에서
        if "fetch" in cmd and "--depth" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=128, stdout="", stderr="boom")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    fetcher = GitRepoFetcher(cache_dir=tmp_path)

    with pytest.raises(RuntimeError, match="git command failed"):
        fetcher.checkout(_make_pr(), installation_token="SECRET-TOKEN")

    # 호출 순서:
    #   1. set-url origin <token URL>      ← token 주입
    #   2. fetch (실패) → RuntimeError
    #   3. **finally** set-url origin <원래 URL>  ← 토큰 복구 (이게 누락되면 회귀)
    set_url_cmds = [c for c in calls if "set-url" in c and "origin" in c]
    assert len(set_url_cmds) == 2, (
        f"set-url 호출이 정확히 2회여야 한다 (token 주입 + 복구). 실제: {len(set_url_cmds)}"
    )
    # 두 번째 호출은 토큰 없는 원래 URL (복구)
    assert "SECRET-TOKEN" not in set_url_cmds[1][-1], (
        "복구 호출의 URL 에 토큰이 들어가면 안 된다 — finally 가 빠지거나 잘못된 인자로 "
        "호출되면 회귀"
    )
    assert set_url_cmds[1][-1] == "https://example/x.git", (
        "복구는 pr.clone_url 그대로여야 한다"
    )


def test_checkout_does_not_call_restore_when_clone_never_creates_dotgit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """clone 이 .git 디렉터리를 만들기도 전에 실패하면 복구 호출 안 함.

    `.git` 존재 여부가 진실 소스 (codex PR #21 review #3). clone 이 시작도 못 한
    케이스 (예: cache_dir 권한 문제로 즉시 실패) 에선 토큰이 디스크에 닿은 적이
    없으므로 set-url 복구 호출도 의미 없는 노이즈. 이 가드가 빠지면 모든 clone 실패
    케이스에 의미 없는 git 호출이 추가되고 그 호출이 또 실패해 fail-fast 의 신호 가치를
    떨어뜨린다.
    """
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "clone" in cmd:
            # .git 만들지 않고 즉시 실패 — 권한/네트워크 즉시 실패 케이스 시뮬
            return subprocess.CompletedProcess(cmd, returncode=128, stdout="", stderr="boom")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    fetcher = GitRepoFetcher(cache_dir=tmp_path)

    with pytest.raises(RuntimeError, match="git command failed"):
        fetcher.checkout(_make_pr(), installation_token="SECRET-TOKEN")

    # .git 이 만들어지지 않았으니 set-url 복구 호출도 없어야 한다
    set_url_cmds = [c for c in calls if "set-url" in c]
    assert set_url_cmds == [], (
        ".git 디렉터리가 만들어지지 않은 clone 실패 케이스에선 복구 호출이 발동하면 안 됨"
    )


def test_checkout_restores_origin_when_clone_partially_writes_dotgit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """clone 이 부분적으로 .git/config 를 만든 뒤 실패해도 토큰 복구가 발동.

    회귀 방지 (codex PR #21 review #3 — **보안**): 이전엔 `token_remote_set` 가
    clone 호출 후에만 True 가 돼 부분 clone 실패 시 finally 가 복구를 건너뛰었다.
    실제 git 은 fetch 단계에서 실패해도 .git/config 에 origin URL (토큰 포함) 을
    남길 수 있어 토큰이 디스크에 평문으로 잔류하는 보안 회귀가 있었다.

    .git 존재 여부 기준으로 정리 트리거 — clone 이 어디서 죽든 부분 .git 만 남으면 정리.
    """
    repo_path = tmp_path / "o" / "r"
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "clone" in cmd:
            # clone 이 .git/config 까지 쓴 뒤 fetch 단계에서 실패하는 시나리오 시뮬:
            # 부분 .git 디렉터리 만든 채 returncode != 0 으로 종료
            (repo_path / ".git").mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(cmd, returncode=128, stdout="", stderr="net")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    fetcher = GitRepoFetcher(cache_dir=tmp_path)

    with pytest.raises(RuntimeError, match="git command failed"):
        fetcher.checkout(_make_pr(), installation_token="SECRET-TOKEN")

    # clone 실패 후에도 .git 이 존재 → finally 가 복구 호출
    set_url_cmds = [c for c in calls if "set-url" in c and "origin" in c]
    assert len(set_url_cmds) == 1, (
        "부분 clone 실패 시에도 토큰 복구가 발동해야 한다 (보안 회귀 방지)"
    )
    # 복구 URL 에 토큰이 들어가면 안 됨
    assert "SECRET-TOKEN" not in set_url_cmds[0][-1]
    assert set_url_cmds[0][-1] == "https://example/x.git"


def test_checkout_normal_path_raises_when_restore_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """정상 경로의 복구 실패는 보안 이슈 → 예외로 승격해 운영자가 인지하게.

    회귀 방지 (codex PR #21 review #4): 이전엔 복구를 무조건 `check=False` 로 호출해
    실패가 silently swallowed 됐다. 그러면 토큰이 디스크에 남아도 호출자는 정상 종료로
    인식해 운영에서 감지 불가. 정상 경로 (다른 git 작업 모두 성공) 의 복구 실패는
    드러내야 — `RuntimeError` 로 raise.
    """
    repo_path = tmp_path / "o" / "r"
    (repo_path / ".git").mkdir(parents=True)

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        # 마지막 set-url (복구) 만 실패. 그 외 모든 git 명령은 성공.
        # 복구 set-url 은 인자 마지막이 토큰 없는 원본 URL — 그걸로 식별.
        if "set-url" in cmd and cmd[-1] == "https://example/x.git":
            return subprocess.CompletedProcess(cmd, returncode=128, stdout="", stderr="boom")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    fetcher = GitRepoFetcher(cache_dir=tmp_path)

    # 모든 git 작업이 성공하고 마지막 복구만 실패 → RuntimeError 가 transparent 하게 raise
    with pytest.raises(RuntimeError, match="git command failed"):
        fetcher.checkout(_make_pr(), installation_token="SECRET-TOKEN")


def test_checkout_preserves_primary_exception_when_restore_also_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: Any,  # pytest.LogCaptureFixture
) -> None:
    """다른 git 작업이 이미 실패한 best-effort 경로의 복구 실패는 ERROR 로그만, 원래 예외 보존.

    회귀 방지: try/finally 패턴에서 finally 가 raise 하면 try 의 원래 예외가 가려진다.
    이는 디버깅 측면에서 큰 손실 — 원래 실패 (예: fetch 권한 오류) 가 사라지고 복구
    실패만 보임. 정상 경로는 (위 테스트) raise 해야 하지만, 이미 raise 중인 경우는
    best-effort 로 낮춰 ERROR 로그로만 남기고 원래 예외 보존.
    """
    import logging

    repo_path = tmp_path / "o" / "r"
    (repo_path / ".git").mkdir(parents=True)

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        # fetch 와 복구 둘 다 실패. 다른 명령은 성공.
        if "fetch" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=128, stdout="", stderr="orig")
        if "set-url" in cmd and cmd[-1] == "https://example/x.git":
            return subprocess.CompletedProcess(cmd, returncode=128, stdout="", stderr="restore-fail")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    fetcher = GitRepoFetcher(cache_dir=tmp_path)

    # 원래 예외 (fetch 실패) 가 raise — 복구 실패가 가리면 안 됨
    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="git command failed") as exc_info:
            fetcher.checkout(_make_pr(), installation_token="SECRET-TOKEN")

    # 메시지가 fetch 실패의 stderr 를 포함 (복구 실패가 아님)
    assert "orig" in str(exc_info.value), (
        "원래 예외가 보존돼야 — 복구 실패가 try 의 예외를 가리면 디버깅 손실"
    )
    # 복구 실패는 ERROR 로그로 남아 운영자가 grep 가능
    error_logs = [
        r for r in caplog.records
        if r.levelname == "ERROR" and "installation token may remain" in r.getMessage()
    ]
    assert len(error_logs) == 1, "복구 실패는 ERROR 로그로 남아야 grep 가능"


# --- 로그 토큰 마스킹 (codex PR #21 review #5 [Critical]) --------------------


def test_mask_auth_in_arg_replaces_credentials_in_https_url() -> None:
    """인증 URL 의 `user:password@` 구간만 `***:***@` 로 치환, 나머지는 보존."""
    masked = _mask_auth_in_arg(
        "https://x-access-token:ghs_SECRETTOKEN123@github.com/owner/repo.git"
    )
    assert "ghs_SECRETTOKEN123" not in masked
    assert "x-access-token" not in masked
    # 도메인과 경로는 디버깅 가치가 있어 유지
    assert "github.com/owner/repo.git" in masked
    assert "***:***@" in masked


def test_mask_auth_in_arg_leaves_non_url_args_untouched() -> None:
    """URL 이 아닌 git 인자 (파일 경로, 플래그, ref 등) 는 그대로 유지."""
    assert _mask_auth_in_arg("--depth") == "--depth"
    assert _mask_auth_in_arg("origin") == "origin"
    assert _mask_auth_in_arg("refs/pull/9/head") == "refs/pull/9/head"
    # 일반 HTTPS URL (credentials 없음) 도 그대로
    assert (
        _mask_auth_in_arg("https://github.com/owner/repo.git")
        == "https://github.com/owner/repo.git"
    )


def test_mask_auth_in_arg_handles_http_not_just_https() -> None:
    """http:// 스킴도 마스킹 — 내부 테스트·dev 환경에서 발생 가능."""
    masked = _mask_auth_in_arg("http://user:pw@localhost:8080/x.git")
    assert "user" not in masked and "pw" not in masked
    assert "localhost:8080/x.git" in masked


def test_checkout_does_not_log_installation_token_in_debug(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DEBUG 로그에 설치 토큰이 평문으로 남지 않는다.

    회귀 방지 (codex PR #21 review #5 [Critical]): 이전 `_run` 은 `cmd[1:]` 전체를
    DEBUG 로 기록했는데 그 중에는 `_inject_token` 이 만든
    `https://x-access-token:<TOKEN>@...` 형태 인증 URL 이 포함. 로그 파일이나 외부
    로그 수집기로 토큰이 유출되는 보안 회귀. `_mask_auth_in_arg` 가 모든 git 명령
    인자를 통과시켜 credentials 구간만 치환해야 한다.
    """
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    fetcher = GitRepoFetcher(cache_dir=tmp_path)

    with caplog.at_level(logging.DEBUG):
        fetcher.checkout(_make_pr(), installation_token="ghs_SECRETTOKEN123")

    # 실제 subprocess 호출에는 토큰이 들어있어야 하지만 (git 이 인증 써야 하니까)
    real_token_in_cmds = any(
        "ghs_SECRETTOKEN123" in " ".join(c) for c in calls
    )
    assert real_token_in_cmds, (
        "subprocess 호출의 실제 인자에는 토큰이 들어 있어야 한다 — 마스킹은 로그에만 적용"
    )

    # 하지만 DEBUG 로그 어디에도 토큰이 평문으로 남아있으면 안 됨
    all_log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "ghs_SECRETTOKEN123" not in all_log_text, (
        "DEBUG 로그에 설치 토큰이 평문으로 남음 — 로그 수집기로 자격 증명 유출 가능"
    )
    # 마스킹 패턴이 실제로 찍혀야 — 빈 로그만 나온 게 아니라는 검증
    assert "***:***@" in all_log_text, (
        "마스킹 패턴이 로그에 보여야 — 아예 URL 인자를 안 찍었으면 디버깅 가치 상실"
    )


def test_run_failure_masks_token_in_stderr_exception_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`git` 실패 시 stderr 가 토큰을 포함해 출력해도 예외 메시지로 토큰이 새지 않는다.

    회귀 방지 (codex PR #21 review #6 [Major]): `git fetch` 등이 인증 실패 시 stderr 에
    토큰 박힌 원격 URL 을 그대로 찍는다. `_run` 이 그 stderr 를 RuntimeError 메시지로 그대로
    감싸면 (예전 동작) 예외 로깅 경로로 토큰이 다시 새는 셈. DEBUG 로그 마스킹과 같은 규칙을
    stderr 에도 적용해 양쪽 다 차단해야 한다.
    """
    repo_path = tmp_path / "o" / "r"
    (repo_path / ".git").mkdir(parents=True)

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        # fetch 호출에서 실패. stderr 에 토큰 박힌 URL 포함 — 실 git 의 인증 실패 메시지 모사.
        if "fetch" in cmd:
            return subprocess.CompletedProcess(
                cmd,
                returncode=128,
                stdout="",
                stderr=(
                    "fatal: unable to access "
                    "'https://x-access-token:ghs_SECRETTOKEN123@github.com/o/r.git/': "
                    "The requested URL returned error: 401"
                ),
            )
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    fetcher = GitRepoFetcher(cache_dir=tmp_path)

    with pytest.raises(RuntimeError, match="git command failed") as exc_info:
        fetcher.checkout(_make_pr(), installation_token="ghs_SECRETTOKEN123")

    msg = str(exc_info.value)
    # stderr 에 박혔던 토큰이 예외 메시지에서 사라져야
    assert "ghs_SECRETTOKEN123" not in msg, (
        "git stderr 의 토큰이 RuntimeError 메시지로 새면 안 됨 — 예외 로그로 자격 증명 유출"
    )
    # 마스킹 패턴이 들어가 있어야 (실제로 stderr 가 마스킹 처리된 흔적)
    assert "***:***@github.com/o/r.git" in msg, (
        "마스킹 패턴이 메시지에 보여야 — stderr 가 통째로 사라지면 디버깅 가치 상실"
    )
    # 401 등 실 진단 정보는 보존돼야 — 마스킹이 너무 광범위하면 디버깅 불가
    assert "401" in msg
    assert "fatal: unable to access" in msg
