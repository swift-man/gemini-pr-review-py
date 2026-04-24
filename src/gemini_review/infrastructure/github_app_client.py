import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

import certifi
import jwt

from gemini_review.domain import (
    Finding,
    PostedReviewComment,
    PullRequest,
    RepoRef,
    ReviewEvent,
    ReviewResult,
)

from .diff_parser import addable_lines_from_patch

logger = logging.getLogger(__name__)

# macOS · python.org 빌드 Python 은 시스템 CA 번들을 자동으로 신뢰하지 않아
# urllib 로 https://api.github.com 호출 시 CERTIFICATE_VERIFY_FAILED 가 뜬다.
# certifi 가 배포하는 루트 번들을 기본값으로 잡아 이 문제를 회피한다. 필요 시
# GitHubAppClient 생성자에 `tls_context=` 를 주입해 교체 가능하다.
def _default_tls_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


# fetch_pull_request 의 head_sha 일관성 재시도 한도. /pulls/{n} → /files 사이 push race
# 가 발생했을 때 재시도. 2회까지 — 그 이상은 force push 폭주 시나리오로 보고 명시적
# 실패시켜 운영자가 알아차리게 한다 (조용한 무한 retry 가 더 위험).
_MAX_FETCH_ATTEMPTS = 3


class _HeadShaChangedMidFetch(Exception):
    """`/files` 페이지네이션 도중 head_sha 가 움직였다는 내부 signal.

    기존 "fetch 전/후 `initial_sha == rechecked_sha` 비교" 만으로는 ABA race 를 잡지
    못한다 — 페이지 1 은 SHA A 기준, 페이지 2 는 SHA B 기준으로 받았지만 마지막
    recheck 시점에 다시 A 로 돌아와 있는 force-push 흐름. 이 경우 changed_files 와
    addable_lines 가 서로 다른 페이지의 혼합 스냅샷이 돼 라인 분할이 어긋난다.

    `_fetch_files_for_pr` 가 페이지 사이에 `/pulls/{n}` 을 찍어 head_sha 가 바뀐 걸
    감지하면 이 예외를 올려 바깥 재시도 루프가 전체 fetch 를 다시 시도하도록 한다.
    (codex PR #19 review #3 대응)
    """

    def __init__(self, initial_sha: str, current_sha: str) -> None:
        super().__init__(
            f"head_sha changed mid-pagination: {initial_sha} -> {current_sha}"
        )
        self.initial_sha = initial_sha
        self.current_sha = current_sha


@dataclass(frozen=True)
class _CachedToken:
    token: str
    expires_at: float

    def is_valid(self) -> bool:
        return time.time() < self.expires_at - 60


class GitHubAppClient:
    """GitHub App installation 으로 인증하는 GitHub REST 클라이언트."""

    def __init__(
        self,
        app_id: int,
        private_key_pem: str,
        api_base: str = "https://api.github.com",
        dry_run: bool = False,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        self._app_id = app_id
        self._private_key = private_key_pem
        self._api_base = api_base.rstrip("/")
        self._dry_run = dry_run
        # DIP: 기본값은 certifi 기반 컨텍스트지만 테스트/다른 CA 번들 주입이 필요할 때
        # 생성자에서 SSLContext 를 교체할 수 있도록 열어 둔다.
        self._tls_context = tls_context or _default_tls_context()
        self._token_cache: dict[int, _CachedToken] = {}

    # --- 인증 ---------------------------------------------------------------

    def _app_jwt(self) -> str:
        # iat 를 30초 과거로 당기고 exp 를 10분 한도(GitHub 제한)에 못 미치는 9분으로 잡는 건
        # 로컬-GitHub 간 시계 오차로 인한 "JWT not yet valid / expired" 실패를 피하기 위함.
        now = int(time.time())
        payload = {"iat": now - 30, "exp": now + 9 * 60, "iss": str(self._app_id)}
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    def get_installation_token(self, installation_id: int) -> str:
        cached = self._token_cache.get(installation_id)
        if cached and cached.is_valid():
            return cached.token

        url = f"{self._api_base}/app/installations/{installation_id}/access_tokens"
        data = self._request_object("POST", url, auth=f"Bearer {self._app_jwt()}")
        token = str(data["token"])
        expires = data.get("expires_at", "")
        # GitHub installation token 은 1시간 유효. 만료 직전 요청이 실패하지 않도록 5분 여유.
        expires_at = time.time() + 55 * 60
        if expires:
            try:
                expires_at = time.mktime(time.strptime(expires, "%Y-%m-%dT%H:%M:%SZ"))
            except ValueError:
                pass
        self._token_cache[installation_id] = _CachedToken(token, expires_at)
        return token

    # --- 공용 API ------------------------------------------------------------

    def fetch_pull_request(
        self, repo: RepoRef, number: int, installation_id: int
    ) -> PullRequest:
        token = self.get_installation_token(installation_id)
        pr_url = f"{self._api_base}/repos/{repo.full_name}/pulls/{number}"

        # ---- head_sha 일관성 재시도 루프 -----------------------------------
        # PR #15 가 "post_review 가 /files 를 다시 부르는" 큰 race 를 닫았지만, 더 작은
        # race 가 fetch_pull_request 안쪽에 남아 있다: GET /pulls/{n} 으로 head_sha 를
        # 받고 GET /files 로 patch 를 받는 그 사이에도 사용자가 push 할 수 있다.
        # 이 경우 PullRequest 에 박힌 head_sha 는 "옛 SHA" 인데 addable_lines/
        # changed_files 는 "새 SHA" 의 것이라 라인 분할이 어긋난다.
        # 해결: /files 끝낸 뒤 /pulls/{n} 을 다시 한 번 짚어 head_sha 가 그대로인지 확인.
        # 변했다면 한 번 더 같은 SHA 로 다시 받도록 재시도. 무한 루프 회피를 위해
        # _MAX_FETCH_ATTEMPTS 로 제한 — force-push 폭주는 운영자가 알아채야 한다.
        #
        # 호출 절감: 재시도 시 직전 iteration 의 `recheck` 결과를 다음 `pr_data` 로 그대로
        # 재사용한다. 이렇게 하면 N 회 시도 시 `/pulls` 호출이 2N 회가 아니라 N+1 회로
        # 줄어든다 (gemini PR #19 review #1).
        pr_data: dict[str, Any] | None = None
        # **마지막 시도** 의 실패 모드를 RuntimeError 메시지/cause 에 반영해 운영자가
        # "ABA 페이지 race" vs "fetch 시작-끝 SHA 변경" 을 구분할 수 있게 한다
        # (codex PR #19 review #4). 두 실패 모드는 디버깅 액션이 다름.
        #
        # 시도마다 reset 해야 — 이전 시도가 mid-pagination 으로 끝나고 마지막 시도가
        # start/end 로 끝났는데도 옛 mid-pagination 예외가 남아 잘못된 모드로 보고되는
        # 회귀를 막는다 (codex PR #19 review #6). 매 iteration 시작에서 None 으로 초기화.
        last_mid_pagination_exc: _HeadShaChangedMidFetch | None = None
        for attempt in range(1, _MAX_FETCH_ATTEMPTS + 1):
            last_mid_pagination_exc = None  # 시도 시작마다 초기화 — 이전 시도의 모드가 새 시도에 안 새도록
            if pr_data is None:
                pr_data = self._request_object("GET", pr_url, auth=f"token {token}")
            initial_sha = str(pr_data["head"]["sha"])

            try:
                changed, addable = self._fetch_files_for_pr(
                    pr_url, token, initial_sha=initial_sha
                )
            except _HeadShaChangedMidFetch as exc:
                # 페이지네이션 도중 head_sha 가 움직였음 — 전체 fetch 재시도.
                # 바깥 initial/rechecked 비교와 별도 (그건 fetch 시작-끝만 커버).
                last_mid_pagination_exc = exc
                if attempt < _MAX_FETCH_ATTEMPTS:
                    logger.warning(
                        "PR %s#%d head_sha changed mid-pagination (%s -> %s); "
                        "restarting fetch (attempt %d/%d)",
                        repo.full_name,
                        number,
                        exc.initial_sha,
                        exc.current_sha,
                        attempt + 1,
                        _MAX_FETCH_ATTEMPTS,
                    )
                # 다음 iteration 은 새 /pulls 호출로 초기화 (이전 pr_data 는 이미 stale).
                pr_data = None
                continue

            # /files 직후 head_sha 재확인. 같으면 그 SHA 의 일관된 스냅샷으로 확정.
            recheck = self._request_object("GET", pr_url, auth=f"token {token}")
            rechecked_sha = str(recheck["head"]["sha"])
            if rechecked_sha == initial_sha:
                # head_sha 가 동일함이 보장됐으므로 changed/addable 는 어느 응답 기준이든
                # 일관됨. 메타데이터(title/body/draft) 는 더 신선한 `recheck` 기준으로
                # 채택해 fetch 도중 변동된 사소한 메타도 누락 없이 반영 (gemini PR #19
                # review #2). head_sha 는 동일하므로 안전.
                head = recheck["head"]
                base = recheck["base"]
                head_sha = str(head["sha"])
                # `_resolve_fetch_source` (PR #21) 는 fork 가 삭제된 PR 도 base.repo +
                # `refs/pull/{n}/head` 로 fetch 가능하도록 (clone_url, fetch_ref) 쌍을 함께
                # 결정한다. 두 값을 동시 결정해야 downstream `GitRepoFetcher` 가 일관된
                # 경로로 PR 스냅샷을 받음.
                clone_url, fetch_ref = _resolve_fetch_source(
                    repo, number, head_sha, head, base
                )
                # GitHub 응답에서 title/body 는 종종 명시적 `null` 로 온다 (예: 본문이
                # 비어 있는 PR). `dict.get(key, default)` 는 키가 있고 값이 None 이면 None
                # 을 그대로 반환하므로 `default` 가 적용되지 않는다. `str(None)` 이 들어가
                # `"None"` 문자열로 오염되는 회귀를 막기 위해 `or ""` 패턴으로 일관 처리
                # (gemini PR #19 review #2 — body 와 동일한 패턴 채택).
                return PullRequest(
                    repo=repo,
                    number=number,
                    title=str(recheck.get("title") or ""),
                    body=str(recheck.get("body") or ""),
                    head_sha=head_sha,
                    head_ref=str(head["ref"]),
                    base_sha=str(base["sha"]),
                    base_ref=str(base["ref"]),
                    clone_url=clone_url,
                    fetch_ref=fetch_ref,
                    changed_files=tuple(changed),
                    installation_id=installation_id,
                    is_draft=bool(recheck.get("draft", False)),
                    addable_lines=tuple(addable),
                )

            # 다음 iteration 은 이 recheck 결과를 시작점으로 — 여분의 /pulls 호출 회피.
            pr_data = recheck

            # 마지막 시도라면 곧바로 RuntimeError 로 빠진다 — "retrying" 표기는 거짓.
            # 실제 재시도가 일어날 때만 로깅 (gemini PR #19 review #2).
            if attempt < _MAX_FETCH_ATTEMPTS:
                logger.warning(
                    "PR %s#%d head_sha changed during fetch (%s -> %s); "
                    "retrying attempt %d/%d to get a consistent snapshot",
                    repo.full_name,
                    number,
                    initial_sha,
                    rechecked_sha,
                    attempt + 1,
                    _MAX_FETCH_ATTEMPTS,
                )

        # 여기에 도달했다는 건 매 시도마다 head_sha 가 바뀌었다는 뜻 — 사용자가
        # 지속적으로 force push 하고 있는 상태. 조용히 잘못된 데이터로 진행하기보다
        # 명시적으로 실패시켜 webhook 큐 자체에서 빼는 게 안전하다 (다음 push 의 새
        # webhook 이 새 시작점이 됨).
        # 실패 모드를 메시지로 구분 (codex PR #19 review #4): 마지막 시도가 페이지네이션
        # 중 ABA race 였다면 그 예외를 cause 로 chain 해 traceback 에 원인 노출. 그렇지
        # 않으면 fetch 시작-끝 SHA 비교에서 실패한 케이스. 두 모드의 디버깅 액션이
        # 다르므로 같은 메시지로 묶이면 운영자 진단을 잘못된 방향으로 유도한다.
        if last_mid_pagination_exc is not None:
            raise RuntimeError(
                f"PR {repo.full_name}#{number} head_sha kept changing across "
                f"{_MAX_FETCH_ATTEMPTS} attempts (last failure: mid-pagination ABA "
                "during /files). Skipping; the next push webhook will retry."
            ) from last_mid_pagination_exc
        raise RuntimeError(
            f"PR {repo.full_name}#{number} head_sha kept changing across "
            f"{_MAX_FETCH_ATTEMPTS} attempts (last failure: fetch start/end SHA "
            "mismatch — push between /pulls and /files). Skipping; the next push "
            "webhook will retry."
        )

    def _fetch_files_for_pr(
        self, pr_url: str, token: str, *, initial_sha: str
    ) -> tuple[list[str], list[tuple[str, frozenset[int]]]]:
        """`/pulls/{n}/files` 를 페이지네이션 끝까지 돌며 (changed, addable) 한 쌍을 만든다.

        한 호출로 두 가지를 동시 수집:
          1) changed_files 목록 (file_collector 우선순위 정렬용)
          2) 각 파일의 patch → addable_lines 사전 파싱 (post_review 인라인 분할용)
        per_page=100 은 GitHub 허용 최대치라 PR 이 큰 경우의 라운드트립 수를 최소화.

        ### ABA race 방어 (codex PR #19 review #3 → #5 보강)

        멀티 페이지 PR 에서 페이지 1 은 SHA A 기준, 페이지 2 는 SHA B 기준 patch 로
        받았더라도 마지막 recheck 시점이 다시 A 라면 바깥 비교는 통과하고 서로 다른
        페이지가 혼합된 스냅샷이 PullRequest 에 박힌다.

        검증 위치는 **각 페이지 응답 직후, 누적 전** (페이지 2+) 으로 잡는다 — 누적된
        데이터가 항상 `initial_sha` 기준임을 보장. 이전 구현은 "다음 페이지 요청 직전"
        에만 검증했는데, 그러면 검증 직후 push 가 발생해 다음 페이지를 다른 SHA 기준으로
        받아오는 창이 남았다 (codex PR #19 review #5).

        **첫 페이지도 검증** (codex PR #19 review #7): 페이지 1 은 `initial_sha` 직후라
        race window 가 좁긴 하지만, 단일 페이지 PR 에서 `initial=A → /files=B → recheck=A`
        형태의 ABA 는 외부 비교만으로는 잡히지 않는다. 비용은 PR 당 `/pulls` 1회 추가.

        **빈 페이지 최적화** (codex PR #19 review #8): `if not files: break` 를 SHA 검증
        보다 먼저 실행해 정확히 100 배수 파일을 가진 PR 이 빈 마지막 페이지에 대해 불필요한
        검증 호출을 하지 않도록 한다. 빈 페이지는 누적될 게 없으니 검증 의미 없음.

        호출 비용 비교:
          - 1 페이지 PR: initial(1) + page1(1) + post-page check(1) + final recheck(1) = 4
          - N 페이지 PR (N>=2): initial(1) + N pages + N post-page checks + final(1) = 2N+2
          - 정확히 100 배수 파일 PR: 빈 페이지는 break 먼저 → check 스킵
        """
        files_url = f"{pr_url}/files?per_page=100"
        changed: list[str] = []
        addable: list[tuple[str, frozenset[int]]] = []
        page = 1
        while True:
            files = self._request_list("GET", f"{files_url}&page={page}", auth=f"token {token}")
            # 빈 페이지 → 누적도 검증도 의미 없음. early-exit.
            if not files:
                break
            # 페이지 응답 직후, 누적 전에 head_sha 검증. 단일 페이지 ABA 까지 커버하려면
            # 첫 페이지도 검증 필요 (codex PR #19 review #7). race window 를 페이지 응답
            # ~ 검증 사이 마이크로초 단위로 축소.
            check = self._request_object("GET", pr_url, auth=f"token {token}")
            current_sha = str(check["head"]["sha"])
            if current_sha != initial_sha:
                raise _HeadShaChangedMidFetch(initial_sha, current_sha)
            for file_entry in files:
                filename = str(file_entry["filename"])
                changed.append(filename)
                # patch 가 None 인 경우(binary, 삭제, GitHub truncate) → 빈 frozenset.
                # 그 결과 해당 파일의 모든 finding 은 본문 surface 로 내려간다 (보수적).
                # patch truncate 정책: GitHub 가 대용량 diff 를 줄여서 보내는 경우, 잘린
                # 영역의 라인은 addable 로 분류되지 않아 인라인 가능했어도 본문 surface
                # 로 내려간다. "인라인 손실 0" 보다 "잘못된 인라인 위치 안 만듦" 을 우선.
                patch = file_entry.get("patch")
                lines = addable_lines_from_patch(
                    patch if isinstance(patch, str) else None
                )
                addable.append((filename, frozenset(lines)))
            # 100개 미만이면 마지막 페이지 — Link 헤더 대신 길이로 단순 판정.
            if len(files) < 100:
                break
            page += 1
        return changed, addable

    def post_review(self, pr: PullRequest, result: ReviewResult) -> None:
        if self._dry_run:
            logger.info("DRY_RUN — review not posted: %s#%d", pr.repo.full_name, pr.number)
            return

        token = self.get_installation_token(pr.installation_id)
        url = f"{self._api_base}/repos/{pr.repo.full_name}/pulls/{pr.number}/reviews"

        # 모델은 전체 코드베이스를 보고 임의 라인을 지적할 수 있지만 GitHub Reviews API 는
        # diff 안 라인에만 인라인 코멘트를 허용한다. fetch_pull_request 시점에 사전 파싱된
        # `pr.addable_lines` 를 사용해 finding 을 두 갈래로 분할:
        #   - inline_findings: 그 집합에 들어가는 것 → 정상 인라인 코멘트로 게시
        #   - surfaced_findings: 그 외 → 본문으로 promote 해 file:line + 등급·내용 노출
        # 사전 분할 덕분에 422 자체가 거의 발생하지 않고, 발생하더라도 retry 가 안전망으로
        # 남는다 (예: GitHub patch 가 truncate 돼서 우리가 "허용" 으로 분류한 라인이 실제론
        # 거부되는 희소 케이스). 캐시를 쓰는 것은 race condition 방지 — 리뷰 생성 도중
        # 사용자가 새 커밋을 push 했을 때 patch 가 갱신돼 모델 finding 의 라인 번호와
        # 불일치하는 사고를 막는다.
        if result.findings:
            inline_findings, surfaced_findings = _partition_findings(
                result.findings, pr.addable_lines_by_path()
            )
        else:
            inline_findings = ()
            surfaced_findings = ()
        if surfaced_findings:
            logger.info(
                "post_review %s#%d: %d inline + %d surfaced (diff 범위 밖)",
                pr.repo.full_name,
                pr.number,
                len(inline_findings),
                len(surfaced_findings),
            )

        # commit_id 를 명시해야 리뷰가 "이 head SHA 시점"에 고정된다. 생략하면 최신 SHA 기준으로
        # 붙어 라인 번호 오정렬이 발생할 수 있다.
        comments = [_finding_to_comment(f) for f in inline_findings]
        payload: dict[str, object] = {
            "commit_id": pr.head_sha,
            "body": result.render_body(surface_findings=surfaced_findings),
            "event": result.event.value,
            "comments": comments,
        }
        try:
            self._request_object("POST", url, auth=f"token {token}", body=payload)
        except urllib.error.HTTPError as exc:
            # 사전 분할이 99% 의 422 를 막지만, 안전망으로 retry 도 유지. 만약 여전히 422 가
            # 나면 inline_findings 안에 우리가 잘못 "허용" 으로 분류한 finding 이 있다는
            # 뜻이므로, 그것들도 본문으로 surface 해서 정보가 사라지지 않게 한다.
            if exc.code != HTTPStatus.UNPROCESSABLE_ENTITY or not comments:
                raise
            logger.warning(
                "review POST returned %d for %s#%d despite pre-filtering; "
                "moving %d remaining inline comments into body and retrying",
                HTTPStatus.UNPROCESSABLE_ENTITY.value,
                pr.repo.full_name,
                pr.number,
                len(inline_findings),
            )
            payload["comments"] = []
            payload["body"] = result.render_body(
                surface_findings=surfaced_findings + inline_findings
            )
            self._request_object("POST", url, auth=f"token {token}", body=payload)

    def post_comment(self, pr: PullRequest, body: str) -> None:
        if self._dry_run:
            logger.info("DRY_RUN — comment not posted: %s#%d", pr.repo.full_name, pr.number)
            return

        token = self.get_installation_token(pr.installation_id)
        url = f"{self._api_base}/repos/{pr.repo.full_name}/issues/{pr.number}/comments"
        self._request_object("POST", url, auth=f"token {token}", body={"body": body})

    def list_self_review_comments(
        self, pr: PullRequest
    ) -> tuple[PostedReviewComment, ...]:
        """본 GitHub App 이 PR 에 게시한 라인 고정 인라인 리뷰 코멘트 목록.

        ### 식별 기준 — `performed_via_github_app.id == self._app_id`

        `user.login` 으로 필터링은 봇 이름 변경에 깨지지만 App ID 는 안정. GitHub
        REST 응답에서 봇이 게시한 코멘트는 `performed_via_github_app` 객체에 App
        식별 메타를 담고 있다. 사람이 게시한 코멘트, 다른 봇 (codex, mlx 등) 의
        코멘트는 자동 제외돼 본 봇의 history grounding 만 비교 대상이 된다.

        ### 제외 케이스 — outdated 코멘트 (`line == null`)

        force-push 후 anchor 가 사라진 코멘트는 GitHub 가 `line=null`, `original_line=N`
        형태로 보존한다. 라인 매칭 dedup 의 key 로 쓸 수 없으므로 인프라 레벨에서
        드롭. `original_line` 으로 fallback 하면 유저가 의도적으로 코드를 옮긴 경우
        가짜 dedup 발동 — 보수적으로 제외.

        ### 페이지네이션 — 최신순 + 1000 cap (codex PR #25 review #2)

        GitHub `/pulls/{n}/comments` 의 기본 정렬은 `created` ASC (오래된 순). 그대로
        `_fetch_files_for_pr` 패턴으로 100 × 10 페이지 cap 을 적용하면 1000 코멘트가
        쌓인 장기 PR 의 **최신** 코멘트가 cap 밖으로 밀려나 dedup history 에서 누락 →
        직전 push 의 finding 도 못 잡는 회귀.

        해결: `sort=created&direction=desc` 로 최신부터 페이지네이션. cap 안에서도 가장
        최근 1000 코멘트를 우선 보장. dedup 의 주된 비교 대상은 직전 push 들의 코멘트
        이므로 최신 1000 만으로 실용 충족 (그 이상 오래된 동일 finding 은 이미 모델 본문
        도 표현이 바뀌어 정확 매칭 확률이 낮음).

        cap 도달 시 WARN — 그 이상이면 운영 이상 신호 (또는 매우 활발한 장기 PR).
        """
        token = self.get_installation_token(pr.installation_id)
        comments_url = (
            f"{self._api_base}/repos/{pr.repo.full_name}/pulls/{pr.number}/comments"
            f"?per_page=100&sort=created&direction=desc"
        )
        results: list[PostedReviewComment] = []
        page = 1
        while page <= 10:
            entries = self._request_list(
                "GET", f"{comments_url}&page={page}", auth=f"token {token}"
            )
            for entry in entries:
                mapped = _map_review_comment(entry, app_id=self._app_id)
                if mapped is not None:
                    results.append(mapped)
            if len(entries) < 100:
                return tuple(results)
            page += 1
        logger.warning(
            "list_self_review_comments capped at 10 pages for %s#%d (>=1000 comments); "
            "dedup may be incomplete",
            pr.repo.full_name,
            pr.number,
        )
        return tuple(results)

    # --- HTTP ---------------------------------------------------------------
    #
    # `_http` 는 인증·직렬화·HTTPError 로깅만 책임지는 저수준 원시 호출. 반환 타입은
    # `Any` 로 열어 두고, GitHub 엔드포인트가 돌려주는 JSON 형태에 따라 아래 두 공용
    # 래퍼가 "객체 vs 배열" 을 경계에서 타입 좁힘 + 런타임 검증한다. 덕분에 호출부는
    # isinstance 분기와 mypy strict 의 `object | list` 유니온 인덱싱 에러에서 자유롭다.

    def _request_object(
        self,
        method: str,
        url: str,
        *,
        auth: str,
        body: object | None = None,
    ) -> dict[str, Any]:
        data = self._http(method, url, auth=auth, body=body)
        if not isinstance(data, dict):
            raise RuntimeError(
                f"expected JSON object from {method} {url}, got {type(data).__name__}"
            )
        return data

    def _request_list(
        self,
        method: str,
        url: str,
        *,
        auth: str,
        body: object | None = None,
    ) -> list[dict[str, Any]]:
        # 최상위만 list 검증하면 배열 안에 dict 가 아닌 값이 섞였을 때 호출부의 `f["key"]` 에서
        # 모호한 TypeError 가 난다. 경계에서 "list 이고 각 항목이 dict" 를 한 번에 보장하면
        # 잘못된 GitHub 응답을 호출부가 아니라 여기서 명확한 메시지로 조기 실패시킬 수 있다.
        data = self._http(method, url, auth=auth, body=body)
        if not isinstance(data, list):
            raise RuntimeError(
                f"expected JSON array from {method} {url}, got {type(data).__name__}"
            )
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                raise RuntimeError(
                    f"expected JSON object at index {i} from {method} {url}, "
                    f"got {type(item).__name__}"
                )
        return data

    def _http(
        self,
        method: str,
        url: str,
        *,
        auth: str,
        body: object | None = None,
    ) -> Any:
        headers = {
            "Authorization": auth,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gemini-review-bot",
        }
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30, context=self._tls_context) as resp:  # noqa: S310
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            # `exc.read()` 는 1회용 stream — 여기서 읽고 나면 호출부가 다시 못 읽는다.
            # 호출부가 422 의 구체 사유(예: "line must be part of the diff" vs 다른 검증
            # 실패) 를 분기에 활용할 수 있도록 읽은 본문을 `gemini_review_detail` 커스텀
            # 속성으로 첨부. 직접 `exc.detail` 같은 표준 속성과 충돌하지 않게 prefix.
            detail = exc.read().decode("utf-8", errors="replace")
            exc.gemini_review_detail = detail  # type: ignore[attr-defined]
            logger.error("GitHub %s %s failed: %s %s", method, url, exc.code, detail[:500])
            raise


def _resolve_fetch_source(
    repo: RepoRef,
    number: int,
    head_sha: str,
    head: dict[str, Any],
    base: dict[str, Any],
) -> tuple[str, str]:
    """PR 을 클론/체크아웃할 `(clone_url, fetch_ref)` 쌍을 안전하게 결정한다.

    정상 케이스:
        return (head.repo.clone_url, head_sha)
    삭제된 fork 케이스 (head.repo == null 또는 clone_url 누락):
        return (base.repo.clone_url, "refs/pull/{number}/head")

    ### 왜 두 값을 같이 결정하는가

    fork 가 삭제된 PR 에서 단순히 clone_url 만 base 로 바꾸면 여전히 `git fetch origin
    {head_sha}` 는 실패한다 — head_sha 는 사라진 fork 에만 있던 객체일 수 있기 때문.
    GitHub 은 base 저장소에 `refs/pull/{number}/head` 라는 virtual ref 를 유지해 PR 의
    마지막 스냅샷을 노출하는데, 이를 통해 삭제된 fork 의 최종 PR 커밋도 받을 수 있다.

    따라서 `clone_url` 을 base 로 바꾸는 순간 `fetch_ref` 도 동시에 `refs/pull/{n}/head`
    로 바꿔야 실제 복구 경로가 된다. 둘이 짝이 되는 결정이라 한 함수에서 같이 반환한다
    (codex PR #21 review #1 대응).

    GitHub PR API 응답에서 `head["repo"]` 는 사용자가 fork 후 그 fork 를 **삭제한 경우
    `null`** 로 온다. 인덱싱 시 TypeError 를 일으켜 fetch 통째로 실패하던 버그를 이
    함수가 graceful fallback 으로 대체한다.

    `repo` 와 `number` 는 fallback 발생 로그에 PR 식별자를 남기기 위해서만 사용.
    """
    head_repo = head.get("repo")
    if isinstance(head_repo, dict):
        clone_url = head_repo.get("clone_url")
        if clone_url:
            return str(clone_url), head_sha

    base_clone_url = str(base["repo"]["clone_url"])
    pr_ref = f"refs/pull/{number}/head"
    logger.warning(
        "PR %s#%d head.repo is missing or has no clone_url (likely deleted fork); "
        "falling back to base.repo.clone_url with ref %s so the PR snapshot is still "
        "fetchable via GitHub's PR refs",
        repo.full_name,
        number,
        pr_ref,
    )
    return base_clone_url, pr_ref


def _finding_to_comment(f: Finding) -> dict[str, object]:
    return {"path": f.path, "line": f.line, "side": "RIGHT", "body": f.body}


def _map_review_comment(
    entry: dict[str, Any], *, app_id: int
) -> PostedReviewComment | None:
    """GitHub `/pulls/{n}/comments` 응답 한 건을 도메인 객체로 매핑하거나 None 으로 드롭.

    드롭 조건 (모두 dedup 시그니처로 부적합한 케이스):
      - 본 App 이 게시한 게 아님 (`performed_via_github_app.id != app_id`)
      - line anchor 가 깨졌음 (`line is None` — force-push 후 outdated)
      - path/body 누락 — GitHub 응답 결손, dedup key 형성 불가

    이 함수가 None 을 반환하는 모든 경로는 의도된 안전한 제외 — 호출자에 별도 신호
    필요 없음. 잘못 매핑되어 false dedup 이 발동하는 것보다 dedup 이 동작하지 않는
    편이 안전 (Layer D 는 방어 레이어).
    """
    via = entry.get("performed_via_github_app")
    if not isinstance(via, dict):
        return None
    if via.get("id") != app_id:
        return None
    line = entry.get("line")
    if not isinstance(line, int):
        return None
    path = entry.get("path")
    body = entry.get("body")
    if not isinstance(path, str) or not isinstance(body, str):
        return None
    return PostedReviewComment(path=path, line=line, body=body)


def _partition_findings(
    findings: tuple[Finding, ...],
    addable_lines: dict[str, frozenset[int]],
) -> tuple[tuple[Finding, ...], tuple[Finding, ...]]:
    """findings 를 (인라인 가능, surfaced) 두 튜플로 분할.

    `(path, line)` 이 `addable_lines` 에 들어가면 인라인. 아니면 surfaced (본문에 노출).
    이 함수가 순서를 보존하므로, 본문 surface 의 표시 순서가 모델이 만든 원래 순서와
    동일하다 — 우선순위 의도가 깨지지 않음. tuple 반환은 ReviewResult.render_body 의
    `surface_findings: tuple[Finding, ...]` 시그니처와 직접 호환되도록.
    """
    inline: list[Finding] = []
    surfaced: list[Finding] = []
    for f in findings:
        # `dict.get(path, set())` 대신 in 체크로 빈 set 객체 생성 회피.
        # finding 수만큼 임시 set 이 생기는 것을 막아 hot path 에서 사소한 메모리 절약.
        if f.path in addable_lines and f.line in addable_lines[f.path]:
            inline.append(f)
        else:
            surfaced.append(f)
    return tuple(inline), tuple(surfaced)


__all__ = ["GitHubAppClient", "ReviewEvent"]
