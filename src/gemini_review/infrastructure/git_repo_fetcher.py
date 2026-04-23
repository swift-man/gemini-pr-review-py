import logging
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from gemini_review.domain import PullRequest

logger = logging.getLogger(__name__)


class GitRepoFetcher:
    """캐시된 저장소를 clone/update 하고 PR 의 head SHA 로 체크아웃한다."""

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir

    def checkout(self, pr: PullRequest, installation_token: str) -> Path:
        repo_path = self._cache_dir / pr.repo.owner / pr.repo.name
        repo_path.parent.mkdir(parents=True, exist_ok=True)

        authed_url = _inject_token(pr.clone_url, installation_token)
        # 토큰 주입 시점부터 .git/config 안에 자격 증명이 들어 있다. 이 함수가 어떤 경로로
        # 빠져나가든 (정상 반환 또는 예외) 항상 원래 URL 로 복구해야 토큰이 디스크에
        # 잔류하지 않는다 (codex PR #21 review #2 — 보안 회귀). clone 케이스에서는
        # 신규 clone 이 끝나기 전에 빠져나갈 일이 없으므로 try 안에 넣어도 동일.
        token_remote_set = False

        try:
            if not (repo_path / ".git").exists():
                logger.info("cloning %s into %s", pr.repo.full_name, repo_path)
                # --filter=blob:none 은 partial clone 으로, 블롭을 지연 로드해 초기 clone 시간과
                # 디스크 사용량을 크게 줄인다. 리뷰엔 checkout 한 SHA 의 blob 만 실제로 받으면 된다.
                _run(["git", "clone", "--filter=blob:none", authed_url, str(repo_path)])
                token_remote_set = True
            else:
                # 설치 토큰은 1시간마다 바뀌므로 기존 remote URL 의 토큰을 교체해야 fetch 가 성공한다.
                _run(
                    ["git", "-C", str(repo_path), "remote", "set-url", "origin", authed_url]
                )
                token_remote_set = True

            # depth=1 로 PR 스냅샷만 얕게 받아 네트워크/디스크 비용을 최소화. 전체 히스토리가 필요
            # 없는 리뷰 용도에 충분하다.
            # `effective_fetch_ref()` 는 보통 `head_sha` 지만, fork 가 삭제된 PR 의 경우
            # `refs/pull/{n}/head` 를 반환해 base 저장소의 GitHub PR ref 로 PR 스냅샷을
            # 받을 수 있게 한다 (`fetch_pull_request` 의 `_resolve_fetch_source` 가 결정).
            # PR ref fetch 시엔 결과 SHA 가 `FETCH_HEAD` 에 들어가므로 checkout 도 그쪽으로.
            fetch_ref = pr.effective_fetch_ref()
            _run(
                ["git", "-C", str(repo_path), "fetch", "--depth", "1", "origin", fetch_ref]
            )
            checkout_target = "FETCH_HEAD" if fetch_ref != pr.head_sha else pr.head_sha
            # --force: 이전 리뷰에서 남은 local modification 이 있어도 무시하고 대상 SHA 로 전환.
            _run(["git", "-C", str(repo_path), "checkout", "--force", checkout_target])
            # -fdx: 추적되지 않는 파일/디렉터리/ignore 대상까지 전부 제거해 이전 체크아웃의 잔여물이
            # 리뷰 입력에 섞이지 않도록 한다.
            _run(["git", "-C", str(repo_path), "clean", "-fdx"])
            return repo_path
        finally:
            # 토큰 주입된 URL 이 .git/config 에 남으면 디스크 자격 증명 노출이라 항상 복구.
            # `check=False` 인 이유: 복구 자체가 실패해도 (예: 저장소가 없거나 손상)
            # 원래 예외를 가리지 말아야 함. 또한 토큰이 들어가지 않은 상태 (clone 실패 직전 등)
            # 에선 굳이 호출할 필요 없음 — `token_remote_set` 가드로 노이즈 회피.
            if token_remote_set:
                _run(
                    [
                        "git", "-C", str(repo_path), "remote", "set-url", "origin",
                        pr.clone_url,
                    ],
                    check=False,
                )


def _inject_token(clone_url: str, token: str) -> str:
    # GitHub 공식 권장 방식: username 자리에 `x-access-token`, password 자리에 installation token.
    # https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/...
    parts = urlsplit(clone_url)
    netloc = f"x-access-token:{token}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _run(cmd: list[str], *, check: bool = True) -> None:
    logger.debug("git %s", " ".join(cmd[1:]))
    result = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git command failed ({result.returncode}): {' '.join(cmd[:2])}...\n"
            f"{result.stderr.strip()}"
        )
