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

from gemini_review.domain import Finding, PullRequest, RepoRef, ReviewEvent, ReviewResult

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
        for attempt in range(1, _MAX_FETCH_ATTEMPTS + 1):
            if pr_data is None:
                pr_data = self._request_object("GET", pr_url, auth=f"token {token}")
            initial_sha = str(pr_data["head"]["sha"])
            changed, addable = self._fetch_files_for_pr(pr_url, token)

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
                return PullRequest(
                    repo=repo,
                    number=number,
                    title=str(recheck.get("title", "")),
                    body=str(recheck.get("body") or ""),
                    head_sha=str(head["sha"]),
                    head_ref=str(head["ref"]),
                    base_sha=str(base["sha"]),
                    base_ref=str(base["ref"]),
                    clone_url=str(head["repo"]["clone_url"]),
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
        raise RuntimeError(
            f"PR {repo.full_name}#{number} head_sha kept changing across "
            f"{_MAX_FETCH_ATTEMPTS} attempts — possibly an active force-push "
            "stream. Skipping this fetch; the next push webhook will retry."
        )

    def _fetch_files_for_pr(
        self, pr_url: str, token: str
    ) -> tuple[list[str], list[tuple[str, frozenset[int]]]]:
        """`/pulls/{n}/files` 를 페이지네이션 끝까지 돌며 (changed, addable) 한 쌍을 만든다.

        한 호출로 두 가지를 동시 수집:
          1) changed_files 목록 (file_collector 우선순위 정렬용)
          2) 각 파일의 patch → addable_lines 사전 파싱 (post_review 인라인 분할용)
        per_page=100 은 GitHub 허용 최대치라 PR 이 큰 경우의 라운드트립 수를 최소화.
        """
        files_url = f"{pr_url}/files?per_page=100"
        changed: list[str] = []
        addable: list[tuple[str, frozenset[int]]] = []
        page = 1
        while True:
            files = self._request_list("GET", f"{files_url}&page={page}", auth=f"token {token}")
            if not files:
                break
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


def _finding_to_comment(f: Finding) -> dict[str, object]:
    return {"path": f.path, "line": f.line, "side": "RIGHT", "body": f.body}


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
