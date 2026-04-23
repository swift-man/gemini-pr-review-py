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
        # `git clone` 은 실패 직전까지 부분 `.git/config` 를 만들어 두므로, clone 중간 실패
        # 만으로도 토큰이 디스크에 평문으로 남을 수 있다. `token_remote_set` 같은 boolean
        # 추적 대신 .git 디렉터리 존재 여부를 진실 소스로 삼는다 — clone 이 어디서 죽든
        # 부분 `.git` 만 남으면 정리 대상으로 인식 (codex PR #21 review #3, gemini suggestion).
        primary_exc: BaseException | None = None

        try:
            if not (repo_path / ".git").exists():
                logger.info("cloning %s into %s", pr.repo.full_name, repo_path)
                # --filter=blob:none 은 partial clone 으로, 블롭을 지연 로드해 초기 clone 시간과
                # 디스크 사용량을 크게 줄인다. 리뷰엔 checkout 한 SHA 의 blob 만 실제로 받으면 된다.
                _run(["git", "clone", "--filter=blob:none", authed_url, str(repo_path)])
            else:
                # 설치 토큰은 1시간마다 바뀌므로 기존 remote URL 의 토큰을 교체해야 fetch 가 성공한다.
                _run(
                    ["git", "-C", str(repo_path), "remote", "set-url", "origin", authed_url]
                )

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
        except BaseException as e:
            primary_exc = e
            raise
        finally:
            # `.git` 존재 = clone 이 어느 정도 진행된 상태 = config 에 토큰이 들어갔을 가능성.
            # 부분 clone 도 잡히고 정상 경로도 잡힌다. 가드 없이 무조건 실행하면 clone 이
            # 시작하기도 전에 실패 (예: cache_dir 권한 문제) 한 케이스에서 의미 없는 git
            # 호출이 노이즈를 만들 수 있어 `.git` 존재 검사로 막는다.
            if (repo_path / ".git").exists():
                _restore_origin_url(repo_path, pr.clone_url, primary_exc)


def _restore_origin_url(
    repo_path: Path,
    original_url: str,
    primary_exc: BaseException | None,
) -> None:
    """`origin` URL 을 토큰 없는 원본으로 되돌린다. 정상 경로 vs 오류 경로 분기 처리.

    설계 (codex PR #21 review #4): `check=False` 로만 처리하면 정상 checkout 경로의 복구
    실패가 조용히 무시돼 토큰 잔류를 운영에서 감지하지 못한다. 분기:

    - **정상 경로 (`primary_exc is None`)**: 복구 실패는 보안 이슈 → RuntimeError 로 raise
      해서 호출자가 인지하도록. 자격 증명 디스크 잔류는 silent fail 보다 loud fail 이 안전.
    - **이미 다른 예외 발생 (`primary_exc is not None`)**: best-effort. 원래 예외를
      가리지 말아야 하므로 raise 안 하고 ERROR 로그만 남긴다 — 운영자가 grep 가능.
    """
    cmd = ["git", "-C", str(repo_path), "remote", "set-url", "origin", original_url]
    try:
        _run(cmd, check=True)
    except RuntimeError as restore_exc:
        if primary_exc is None:
            # 정상 경로: 복구 실패는 raise 해서 보안 회귀를 인지하게 한다.
            raise
        # 이미 raise 중인 예외가 있음 — best-effort, 원래 예외 보존
        logger.error(
            "failed to restore origin URL for %s after primary failure; "
            "installation token may remain in .git/config: %s",
            repo_path,
            restore_exc,
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
