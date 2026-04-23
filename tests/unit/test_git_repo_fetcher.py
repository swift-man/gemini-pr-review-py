"""GitRepoFetcher 의 fetch ref 라우팅 단위 테스트.

핵심: PullRequest.fetch_ref 가 비어있으면 head_sha 로 fallback (역호환), 비어있지 않으면
그 ref 로 fetch + FETCH_HEAD 로 checkout. fork 가 삭제된 PR 에서 base.repo 의
`refs/pull/{n}/head` 로 PR 스냅샷을 받는 경로의 회귀 방지 (codex PR #21 review #1).
"""
import subprocess
from pathlib import Path
from typing import Any

import pytest

from gemini_review.domain import PullRequest, RepoRef
from gemini_review.infrastructure.git_repo_fetcher import GitRepoFetcher


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


def test_checkout_does_not_call_restore_when_token_was_never_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """clone 자체가 실패한 경우엔 set-url 복구 호출도 없어야 노이즈 안 생김.

    .git 디렉터리가 없어서 clone 경로로 갔지만 clone 자체가 실패하면 token 이 세팅된
    상태가 아니므로 finally 의 복구 호출은 의미가 없다 (`token_remote_set` 가드 회귀
    방지). check=False 라 복구 실패는 오류로는 안 나타나지만, 불필요한 git 호출은
    로그·디스크 IO 측면에서 노이즈.
    """
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "clone" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=128, stdout="", stderr="boom")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    fetcher = GitRepoFetcher(cache_dir=tmp_path)

    with pytest.raises(RuntimeError, match="git command failed"):
        fetcher.checkout(_make_pr(), installation_token="SECRET-TOKEN")

    # clone 만 시도되고 그 외 git 호출은 없어야
    set_url_cmds = [c for c in calls if "set-url" in c]
    assert set_url_cmds == [], (
        "clone 자체 실패 시엔 set-url 복구 호출이 발동하지 않아야 한다"
    )
