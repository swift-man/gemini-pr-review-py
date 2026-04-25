import io
import json
import logging
import ssl
import urllib.error
import urllib.request
from typing import Any

import jwt
import pytest

from gemini_review.domain import (
    Finding,
    PullRequest,
    RepoRef,
    ReviewEvent,
    ReviewResult,
)
from gemini_review.infrastructure import github_app_client
from gemini_review.infrastructure.github_app_client import GitHubAppClient


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """urlopen 과 jwt.encode 를 가로채어 GitHubAppClient 의 호출 배선을 검증한다.

    실제 네트워크 호출이나 실 RSA 키 없이도 "어떤 URL, 어떤 TLS 컨텍스트, 어떤
    timeout 으로 호출했는가" 를 테스트에서 관찰할 수 있게 해준다.
    """
    sink: dict[str, Any] = {}

    def fake_urlopen(
        req: urllib.request.Request,
        *,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> _FakeResponse:
        sink["url"] = req.full_url
        sink["timeout"] = timeout
        sink["context"] = context
        return _FakeResponse(b'{"token": "tkn", "expires_at": ""}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")
    return sink


def test_request_passes_injected_tls_context_to_urlopen(captured: dict[str, Any]) -> None:
    """회귀 방지: `_request()` 는 `ssl.SSLContext` 를 `urlopen(context=...)` 로 전달해야 한다.
    이걸 빠뜨리면 python.org 빌드 Python 에서 CERTIFICATE_VERIFY_FAILED 로 파이프라인이 죽는다.
    """
    injected = ssl.create_default_context()
    client = GitHubAppClient(app_id=1, private_key_pem="-", tls_context=injected)

    client.get_installation_token(installation_id=42)

    assert captured["context"] is injected
    assert "installations/42/access_tokens" in captured["url"]


def test_default_tls_context_is_a_verifying_sslcontext(captured: dict[str, Any]) -> None:
    """기본 TLS 컨텍스트는 인증서 검증을 켠 상태여야 한다.
    (certifi 번들을 끄거나 검증을 비활성화하는 회귀를 잡는다.)
    """
    client = GitHubAppClient(app_id=1, private_key_pem="-")

    client.get_installation_token(installation_id=42)

    ctx = captured["context"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_default_tls_context_factory_is_fresh_instance() -> None:
    """생성자 기본값이 싱글톤 모듈 변수가 아니라 팩토리 함수로 만들어지는지 확인.
    덕분에 테스트·환경별로 독립된 SSLContext 를 가질 수 있다.
    """
    a = github_app_client._default_tls_context()
    b = github_app_client._default_tls_context()
    assert a is not b


def _stub_response(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> None:
    """HTTP 경로를 stub 해서 `_request_*` 경계 검증만 단독으로 테스트한다."""
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_a, **_k: _FakeResponse(payload),
    )


def test_request_list_raises_when_response_is_not_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub 가 배열을 약속한 엔드포인트에서 객체/프리미티브를 반환하면 즉시 실패해야 한다."""
    _stub_response(monkeypatch, b'{"message": "rate limited"}')
    client = GitHubAppClient(app_id=1, private_key_pem="-")

    with pytest.raises(RuntimeError, match="expected JSON array"):
        client._request_list("GET", "https://api.github.com/x", auth="token t")


def test_request_list_raises_when_item_is_not_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """배열이지만 내부에 dict 가 아닌 값이 섞이면 호출부의 `f["key"]` 전에 조기 실패."""
    _stub_response(monkeypatch, b'[{"filename": "a.py"}, "broken"]')
    client = GitHubAppClient(app_id=1, private_key_pem="-")

    with pytest.raises(RuntimeError, match="expected JSON object at index 1"):
        client._request_list("GET", "https://api.github.com/x", auth="token t")


def test_request_object_raises_when_response_is_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """객체를 기대하는 엔드포인트에서 배열이 오면 마찬가지로 조기 실패."""
    _stub_response(monkeypatch, b'[1, 2, 3]')
    client = GitHubAppClient(app_id=1, private_key_pem="-")

    with pytest.raises(RuntimeError, match="expected JSON object"):
        client._request_object("GET", "https://api.github.com/x", auth="token t")


def _sample_pr(
    *,
    addable_lines: tuple[tuple[str, frozenset[int]], ...] = (),
) -> PullRequest:
    """Test 용 PullRequest 빌더. `addable_lines` 는 fetch_pull_request 시점에 사전
    파싱된 결과를 시뮬레이션 — race condition 방지를 위해 post_review 가 이 캐시만
    참조하므로 테스트도 이 필드로 인라인/surface 분기를 지정한다.
    """
    return PullRequest(
        repo=RepoRef("o", "r"),
        number=9,
        title="t",
        body="",
        head_sha="abc",
        head_ref="feat",
        base_sha="def",
        base_ref="main",
        clone_url="https://example/x.git",
        changed_files=("a.py",),
        installation_id=7,
        is_draft=False,
        addable_lines=addable_lines,
    )


def _files_response(patches: dict[str, str | None]) -> bytes:
    """`/pulls/{n}/files` 응답 바디를 만들어 주는 테스트 헬퍼.

    각 파일명에 대해 `{filename, patch}` 객체 배열로 직렬화. patch=None 인 경우는
    binary/삭제/truncated 파일을 시뮬레이션하기 위함.
    """
    items = [{"filename": name, "patch": patch} for name, patch in patches.items()]
    return json.dumps(items).encode()


def _make_fake_urlopen(
    posted_bodies: list[dict[str, Any]],
    patches: dict[str, str | None],
    fail_first_review_with_422: bool = False,
):
    """post_review 흐름을 가짜 GitHub 으로 시뮬레이션하는 urlopen 대체.

    - access_tokens: 정상 토큰 응답
    - /pulls/{n}/files: 주어진 patches 를 응답
    - /pulls/{n}/reviews: posted_bodies 에 캡처. fail_first_review_with_422=True 면
      첫 호출만 422 raise.
    """

    def fake_urlopen(
        req: urllib.request.Request,
        *,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> _FakeResponse:
        if "access_tokens" in req.full_url:
            return _FakeResponse(b'{"token": "tkn", "expires_at": ""}')
        if "/files" in req.full_url:
            return _FakeResponse(_files_response(patches))
        # review POST
        assert req.data is not None
        posted_bodies.append(json.loads(req.data.decode("utf-8")))
        if fail_first_review_with_422 and len(posted_bodies) == 1:
            raise urllib.error.HTTPError(
                req.full_url,
                422,
                "Unprocessable Entity",
                {},  # type: ignore[arg-type]
                io.BytesIO(b'{"message": "Validation Failed"}'),
            )
        return _FakeResponse(b'{"id": 1}')

    return fake_urlopen


def test_post_review_partitions_findings_into_inline_and_surfaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """addable_lines 에 따라 finding 이 inline 과 surfaced 로 정확히 분할된다.

    핵심 회귀 방지: 사전 분할이 깨지면 (1) 422 가 다시 발생하거나 (2) 본문 surface
    가 누락되거나 (3) 같은 finding 이 두 곳에 중복 노출되는 사고가 일어난다.
    addable_lines 는 fetch_pull_request 시점에 캐시된 값 — post_review 는 추가로
    /files 를 호출하지 않는다 (race condition 방지).
    """
    posted_bodies: list[dict[str, Any]] = []
    monkeypatch.setattr(
        urllib.request, "urlopen", _make_fake_urlopen(posted_bodies, {})
    )
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.REQUEST_CHANGES,
        positives=("좋음",),
        improvements=("개선",),
        findings=(
            Finding(path="a.py", line=5, body="[Major] 라인 5 — 인라인 가능"),
            Finding(path="a.py", line=42, body="[Critical] 라인 42 — diff 밖"),
        ),
    )
    pr = _sample_pr(addable_lines=(("a.py", frozenset({5, 6})),))

    client.post_review(pr, result)

    # 단일 POST — 422 retry 발동 안 함
    assert len(posted_bodies) == 1, "사전 분할이 정확하면 retry 가 일어나면 안 된다"

    # comments 에는 line 5 만
    posted = posted_bodies[0]
    assert len(posted["comments"]) == 1
    assert posted["comments"][0]["path"] == "a.py"
    assert posted["comments"][0]["line"] == 5

    # body 에 line 42 가 surface 됨
    body = str(posted["body"])
    assert "a.py:42" in body
    assert "[Critical] 라인 42" in body
    # 인라인 카운트 안내는 inline_findings 길이 = 1
    assert "기술 단위 코멘트 1건" in body
    # surface 안내
    assert "1개 코멘트는 PR diff 범위 밖" in body


def test_post_review_all_inline_when_all_lines_addable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """모든 finding 이 addable 라인을 가리키면 surface 섹션 없이 인라인만 게시."""
    posted_bodies: list[dict[str, Any]] = []
    monkeypatch.setattr(
        urllib.request, "urlopen", _make_fake_urlopen(posted_bodies, {})
    )
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.COMMENT,
        findings=(
            Finding(path="a.py", line=1, body="[Minor] x"),
            Finding(path="a.py", line=2, body="[Minor] y"),
        ),
    )
    pr = _sample_pr(addable_lines=(("a.py", frozenset({1, 2, 3})),))

    client.post_review(pr, result)

    body = str(posted_bodies[0]["body"])
    assert len(posted_bodies[0]["comments"]) == 2
    assert "기술 단위 코멘트 2건은 각 라인에 별도 표시" in body
    assert "드롭된 라인 지적" not in body  # surface 섹션 없음


def test_post_review_all_surfaced_when_no_addable_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """파일이 addable 라인 없음 (binary 등) 이면 모든 finding 이 surface — 인라인 0건."""
    posted_bodies: list[dict[str, Any]] = []
    monkeypatch.setattr(
        urllib.request, "urlopen", _make_fake_urlopen(posted_bodies, {})
    )
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.COMMENT,
        findings=(
            Finding(path="binary.png", line=1, body="[Minor] meta data"),
        ),
    )
    pr = _sample_pr(addable_lines=(("binary.png", frozenset()),))  # 빈 frozenset

    client.post_review(pr, result)

    posted = posted_bodies[0]
    assert posted["comments"] == []
    body = str(posted["body"])
    assert "binary.png:1" in body
    assert "[Minor] meta data" in body


def test_post_review_uses_cached_addable_lines_no_extra_files_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**race condition 회귀 방지**: post_review 가 추가로 /files 를 호출하면 안 된다.

    리뷰 생성 도중 사용자가 새 커밋을 push 한 시나리오를 시뮬레이션 — 두 번째 /files
    응답에는 다른 patch 가 들어있다고 가정. post_review 가 실수로 /files 를 다시 부르면
    그 응답을 받아 라인 분류가 달라지므로, 이 테스트가 호출 자체를 감지하고 실패한다.
    """
    files_call_count = 0

    def fake_urlopen(
        req: urllib.request.Request,
        *,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> _FakeResponse:
        nonlocal files_call_count
        if "access_tokens" in req.full_url:
            return _FakeResponse(b'{"token": "tkn", "expires_at": ""}')
        if "/files" in req.full_url:
            files_call_count += 1
            # 일부러 다른 데이터로 응답 — post_review 가 호출했다면 잘못된 분류로 이어짐
            return _FakeResponse(
                b'[{"filename": "a.py", "patch": "@@ -1 +1 @@\\n+stale\\n"}]'
            )
        # review POST 는 정상 응답
        return _FakeResponse(b'{"id": 1}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.COMMENT,
        findings=(Finding(path="a.py", line=5, body="[Minor] x"),),
    )
    # 캐시된 addable_lines 만 신뢰해야 함
    pr = _sample_pr(addable_lines=(("a.py", frozenset({5})),))

    client.post_review(pr, result)

    assert files_call_count == 0, (
        "post_review 가 /files 를 호출하면 race condition 방지 의도가 무너진다"
    )


def test_post_review_safety_net_moves_inline_to_body_on_unexpected_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """사전 분할이 잘못 판정한 희소 케이스 (예: GitHub patch truncate) — 422 가 나면
    남은 inline 들도 body 로 옮기고 retry. 정보 손실 없이 게시 보장."""
    posted_bodies: list[dict[str, Any]] = []
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _make_fake_urlopen(posted_bodies, {}, fail_first_review_with_422=True),
    )
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.REQUEST_CHANGES,
        findings=(
            Finding(path="a.py", line=1, body="[Critical] 우리는 addable 이라 봤지만 GitHub 가 거부"),
        ),
    )
    # 우리 분할은 line 1 이 addable 이라고 판정 — 그래서 1차 POST 에 인라인으로 들어간다.
    # 하지만 fake GitHub 가 422 로 거부 (예: 실제론 patch truncate 였던 케이스 시뮬레이션)
    pr = _sample_pr(addable_lines=(("a.py", frozenset({1})),))

    # 예외 삼켜져야 함 (retry 성공)
    client.post_review(pr, result)

    assert len(posted_bodies) == 2, "1차 + retry = 2회"
    # 1차: 우리 분할 결과대로 inline 1건 시도
    assert len(posted_bodies[0]["comments"]) == 1
    # 2차: comments 비우고 그 내용을 body 로 surface
    assert posted_bodies[1]["comments"] == []
    body_retry = str(posted_bodies[1]["body"])
    assert "a.py:1" in body_retry
    assert "[Critical] 우리는 addable" in body_retry


def test_post_review_does_not_retry_when_no_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """comments 가 처음부터 비어 있었다면 422 는 다른 원인이므로 재시도하지 않는다.

    재시도가 같은 payload 로 반복되는 무한 루프를 막고, 진짜 원인(예: 잘못된 commit_id,
    invalid event) 이 로그와 예외로 드러나도록 유지한다.
    """
    call_count = 0

    def fake_urlopen(
        req: urllib.request.Request,
        *,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> _FakeResponse:
        nonlocal call_count
        if "access_tokens" in req.full_url:
            return _FakeResponse(b'{"token": "tkn", "expires_at": ""}')
        call_count += 1
        raise urllib.error.HTTPError(
            req.full_url,
            422,
            "Unprocessable Entity",
            {},  # type: ignore[arg-type]
            io.BytesIO(b'{"message": "Validation Failed"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.COMMENT,
        # findings 없음
    )

    with pytest.raises(urllib.error.HTTPError) as exc:
        client.post_review(_sample_pr(), result)

    assert exc.value.code == 422
    assert call_count == 1, "재시도 없이 첫 실패에서 종료돼야 함"


def test_post_review_does_not_retry_on_non_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """422 가 아닌 다른 HTTP 에러(예: 404, 401, 500)는 그대로 전파."""
    call_count = 0

    def fake_urlopen(
        req: urllib.request.Request,
        *,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> _FakeResponse:
        nonlocal call_count
        if "access_tokens" in req.full_url:
            return _FakeResponse(b'{"token": "tkn", "expires_at": ""}')
        call_count += 1
        raise urllib.error.HTTPError(
            req.full_url,
            500,
            "Internal Server Error",
            {},  # type: ignore[arg-type]
            io.BytesIO(b'{"message": "boom"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.COMMENT,
        findings=(Finding(path="a.py", line=5, body="x"),),
    )
    pr = _sample_pr(addable_lines=(("a.py", frozenset({5})),))

    with pytest.raises(urllib.error.HTTPError) as exc:
        client.post_review(pr, result)

    assert exc.value.code == 500
    assert call_count == 1


def test_fetch_pull_request_collects_addable_lines_with_single_files_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fetch_pull_request 가 /files 한 번 호출로 changed_files + addable_lines 를 동시 수집.

    회귀 방지: 이걸 두 번 나눠 호출하면 (1) 라운드트립 낭비 (2) race condition 위험
    (두 번째 호출이 새 head_sha 의 patch 를 받을 수 있음). 한 호출에서 함께 처리하는
    것이 일관성 보장의 핵심. (/pulls 는 head_sha 일관성 재확인 때문에 2회 호출됨 —
    별도의 race 테스트에서 검증.)
    """
    files_call_count = 0

    def fake_urlopen(
        req: urllib.request.Request,
        *,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> _FakeResponse:
        nonlocal files_call_count
        if "access_tokens" in req.full_url:
            return _FakeResponse(b'{"token": "tkn", "expires_at": ""}')
        if "/files" in req.full_url:
            files_call_count += 1
            return _FakeResponse(json.dumps([
                {"filename": "src/a.py", "patch": "@@ -1,0 +5,2 @@\n+x\n+y\n"},
                {"filename": "binary.png", "patch": None},  # binary file
            ]).encode())
        # /pulls/{n} 메타데이터
        return _FakeResponse(json.dumps({
            "title": "t",
            "body": "b",
            "draft": False,
            "head": {"sha": "abc", "ref": "feat", "repo": {"clone_url": "https://x.git"}},
            "base": {"sha": "def", "ref": "main"},
        }).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    pr = client.fetch_pull_request(RepoRef("o", "r"), 9, installation_id=7)

    # 단일 /files 호출 — 두 번 부르면 race condition 위험
    assert files_call_count == 1

    # changed_files 와 addable_lines 모두 채워졌어야
    assert pr.changed_files == ("src/a.py", "binary.png")
    addable = pr.addable_lines_by_path()
    assert addable["src/a.py"] == frozenset({5, 6})
    # binary 파일은 patch=None → 빈 frozenset (보수적)
    assert addable["binary.png"] == frozenset()

    # file_patches 도 같은 호출에서 함께 수집됐어야 — diff fallback 입력 (binary 는 제외)
    patches_by_path = dict(pr.file_patches)
    assert patches_by_path == {"src/a.py": "@@ -1,0 +5,2 @@\n+x\n+y\n"}, (
        "binary/None patch 파일은 file_patches 에서 제외 — diff fallback 의 입력 후보 X"
    )


def _build_fake_urlopen_for_fetch_pr(
    *,
    head_shas: list[str],
    files_payload: bytes = b'[{"filename": "src/a.py", "patch": "@@ -1,0 +5,2 @@\\n+x\\n+y\\n"}]',
    counters: dict[str, int] | None = None,
):
    """fetch_pull_request race-condition 테스트용 urlopen 빌더.

    `head_shas` 는 `/pulls/{n}` 호출이 매번 어떤 head_sha 를 반환할지 순서대로 지정.
    반복 시 마지막 값을 계속 사용 (테스트가 시도 횟수보다 짧은 리스트를 줘도 안전).
    """
    if counters is None:
        counters = {"pulls": 0, "files": 0}

    def fake_urlopen(
        req: urllib.request.Request,
        *,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> _FakeResponse:
        if "access_tokens" in req.full_url:
            return _FakeResponse(b'{"token": "tkn", "expires_at": ""}')
        if "/files" in req.full_url:
            counters["files"] += 1
            return _FakeResponse(files_payload)
        # /pulls/{n} 메타데이터 — head_sha 만 시퀀스에 따라 바꾸고 나머진 고정
        idx = min(counters["pulls"], len(head_shas) - 1)
        sha = head_shas[idx]
        counters["pulls"] += 1
        return _FakeResponse(json.dumps({
            "title": "t",
            "body": "b",
            "draft": False,
            "head": {"sha": sha, "ref": "feat", "repo": {"clone_url": "https://x.git"}},
            "base": {"sha": "def", "ref": "main"},
        }).encode())

    return fake_urlopen, counters


def test_fetch_pull_request_rechecks_head_sha_after_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/files` 직후 `/pulls/{n}` 을 다시 짚어 head_sha 가 그대로인지 확인해야 한다.

    회귀 방지: 이 재확인이 빠지면 `/pulls/{n}` 과 `/files` 사이 사용자가 push 한
    경우 head_sha 는 옛 SHA, addable_lines 는 새 SHA 의 것이라 라인 분할이 어긋나
    잘못된 위치에 인라인 코멘트가 붙는다.
    """
    fake, counters = _build_fake_urlopen_for_fetch_pr(head_shas=["abc"])
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    pr = client.fetch_pull_request(RepoRef("o", "r"), 9, installation_id=7)

    # /pulls/{n} 호출 3회: (1) initial, (2) post-page check (첫 페이지 응답 직후 검증,
    # codex PR #19 review #7), (3) final recheck. 검증 호출이 누락되면 3 → 2 로 떨어져
    # 이 assertion 이 잡아낸다.
    assert counters["pulls"] == 3, "head_sha 일관성 재확인 호출이 누락됐다"
    assert counters["files"] == 1, "정상 케이스에선 /files 가 한 번만 호출돼야 한다"
    assert pr.head_sha == "abc"


def test_fetch_pull_request_normalizes_null_title_and_body_to_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub 응답에서 title/body 가 `null` 이면 빈 문자열로 매핑.

    회귀 방지 (gemini PR #19 review #2): `dict.get("title", "")` 는 키가 존재하고 값이
    None 이면 None 을 반환한다. 그 결과 `str(None) == "None"` 이 PullRequest.title 에
    박혀 다운스트림 (프롬프트, 본문 surface 등) 에 "None" 이 노출되는 회귀가 생긴다.
    `or ""` 패턴으로 None 도 안전하게 빈 문자열로 처리해야 한다.
    """

    def fake_urlopen(
        req: urllib.request.Request,
        *,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> _FakeResponse:
        if "access_tokens" in req.full_url:
            return _FakeResponse(b'{"token": "tkn", "expires_at": ""}')
        if "/files" in req.full_url:
            return _FakeResponse(
                b'[{"filename": "a.py", "patch": "@@ -1,0 +1,1 @@\\n+x\\n"}]'
            )
        # title 과 body 모두 명시적 null — 빈 본문 PR 이 GitHub 에서 이런 응답을 준다.
        return _FakeResponse(json.dumps({
            "title": None,
            "body": None,
            "draft": False,
            "head": {"sha": "abc", "ref": "feat", "repo": {"clone_url": "https://x.git"}},
            "base": {"sha": "def", "ref": "main"},
        }).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    pr = client.fetch_pull_request(RepoRef("o", "r"), 9, installation_id=7)

    # "None" 문자열이 박히면 회귀 — 빈 문자열이어야
    assert pr.title == "", "null title 은 빈 문자열로 매핑돼야 (str(None) 회귀 방지)"
    assert pr.body == "", "null body 도 빈 문자열로 매핑돼야"


def test_fetch_pull_request_uses_recheck_metadata_when_head_sha_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """head_sha 동일하면 더 신선한 `recheck` 응답의 메타데이터(title/body/draft)를 채택.

    회귀 방지 (gemini PR #19 review #2): fetch 도중 사용자가 head 는 안 바꾸고 PR 본문/
    제목/draft 상태만 갱신하는 경우, `pr_data` 기준으로 PullRequest 를 만들면 옛 메타가
    박힌다. head_sha 가 같다는 것은 changed/addable 일관성만 보장하면 되고, 메타는 더
    신선한 쪽을 쓰는 게 자연스럽다.
    """
    pulls_call_count = 0

    def fake_urlopen(
        req: urllib.request.Request,
        *,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> _FakeResponse:
        nonlocal pulls_call_count
        if "access_tokens" in req.full_url:
            return _FakeResponse(b'{"token": "tkn", "expires_at": ""}')
        if "/files" in req.full_url:
            return _FakeResponse(
                b'[{"filename": "src/a.py", "patch": "@@ -1,0 +5,2 @@\\n+x\\n+y\\n"}]'
            )
        # /pulls/{n}: 첫 호출 = 옛 메타, 두 번째 호출 = 새 메타 (head_sha 동일).
        pulls_call_count += 1
        is_initial = pulls_call_count == 1
        return _FakeResponse(json.dumps({
            "title": "옛 제목" if is_initial else "새 제목",
            "body": "옛 본문" if is_initial else "새 본문",
            "draft": True if is_initial else False,
            "head": {"sha": "abc", "ref": "feat", "repo": {"clone_url": "https://x.git"}},
            "base": {"sha": "def", "ref": "main"},
        }).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    pr = client.fetch_pull_request(RepoRef("o", "r"), 9, installation_id=7)

    # head_sha 동일성은 유지
    assert pr.head_sha == "abc"
    # 메타데이터는 두 번째(recheck) 응답 기준 — 옛 메타가 박히면 회귀
    assert pr.title == "새 제목"
    assert pr.body == "새 본문"
    assert pr.is_draft is False


def test_fetch_pull_request_retries_when_head_sha_changes_mid_fetch(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """fetch 시작-끝 사이 head_sha 가 바뀌면 재시도해 일관된 스냅샷을 얻어야 한다.

    회귀 방지: 재시도 로직이 빠지면 첫 시도의 어긋난 데이터를 그대로 PullRequest 에
    박아 후속 인라인 분할이 깨진다. 두 번째 시도에서 head_sha 가 안정되면 그 SHA 의
    일관된 스냅샷으로 정상 반환해야 한다.

    시나리오는 **start/end 모드** 를 deliberately 발동 — 단일 페이지 PR 에서 post-page
    check 는 match 하되 final recheck 에서 mismatch. mid-pagination 과 분리된 경로.
    """
    # 시나리오:
    #   1차: pulls=abc (initial) → files → pulls=abc (post-page, match) → pulls=def (recheck, mismatch)
    #        → retry, pr_data = recheck (def)
    #   2차: (carried pr_data, sha=def) → files → pulls=def (post-page) → pulls=def (recheck, match)
    #        → return
    fake, counters = _build_fake_urlopen_for_fetch_pr(
        head_shas=["abc", "abc", "def", "def", "def"]
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    with caplog.at_level(logging.WARNING):
        pr = client.fetch_pull_request(RepoRef("o", "r"), 9, installation_id=7)

    # /pulls 호출 5회: 1차 (initial + post-page + recheck) + 2차 carry init skip
    # (post-page + recheck) = 3+2 = 5. recheck 재사용 최적화가 깨지면 6+ 이 될 것.
    assert counters["pulls"] == 5
    assert counters["files"] == 2
    # 두 번째 시도의 안정된 SHA 가 박혀야 한다
    assert pr.head_sha == "def"

    # WARN 로그가 운영 관측 — race 가 일어났음을 운영자가 알 수 있어야
    warns = [r for r in caplog.records if "head_sha changed" in r.getMessage()]
    assert len(warns) == 1, "head_sha race 발생 시 WARN 한 줄이 남아야 한다"
    assert "abc" in warns[0].getMessage() and "def" in warns[0].getMessage()


def test_fetch_pull_request_raises_when_head_sha_keeps_changing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """매 시도마다 start/end 모드로 실패하면 무한 retry 대신 명시적 실패.

    조용히 잘못된 데이터로 진행하는 것이 가장 위험한 실패 모드 — 차라리 webhook 큐
    수준에서 빼버리고 다음 push 의 새 webhook 이 새 시작점이 되도록 하는 게 안전.

    회귀 방지:
    - 마지막 시도에서 "retrying" 로그가 더 안 찍히는지 (gemini PR #19 review #2)
    - 실패 메시지가 fetch 시작-끝 모드임을 명시 (codex PR #19 review #4) — ABA 페이지
      race 와 다른 디버깅 경로이므로 진단 메시지가 구분돼야
    - `__cause__` is None (mid-pagination 예외 미발생 시 chain 안 됨)

    시나리오: 단일 페이지 PR 에서 post-page 는 match 하되 매 attempt 의 final recheck
    에서 mismatch. 즉 race 가 fetch 시작-끝 구간에서만 발생하는 경우.
    """
    # 매 attempt 에서 post-page 는 초기 SHA 와 match, final recheck 는 다른 SHA 로.
    # 시퀀스: [init1, post1, recheck1, post2(after carry), recheck2, post3, recheck3]
    fake, _counters = _build_fake_urlopen_for_fetch_pr(
        head_shas=["s1", "s1", "s2", "s2", "s3", "s3", "s4"]
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError, match="head_sha kept changing") as exc_info:
            client.fetch_pull_request(RepoRef("o", "r"), 9, installation_id=7)

    # 진단 메시지: fetch 시작-끝 SHA 변동. post-page 는 매번 match 했으므로 mid-pagination
    # 경로는 한 번도 안 밟음.
    assert "fetch start/end SHA mismatch" in str(exc_info.value)
    # ABA cause 가 chain 되면 안 됨 (mid-pagination 예외 미발생)
    assert exc_info.value.__cause__ is None, (
        "mid-pagination 예외가 발생하지 않았으니 cause chain 도 없어야"
    )

    # _MAX_FETCH_ATTEMPTS=3 → 1차·2차 mismatch 시 "retrying" 로그, 3차는 곧바로 raise.
    retry_warns = [r for r in caplog.records if "retrying attempt" in r.getMessage()]
    assert len(retry_warns) == 2, (
        "마지막 시도에선 retry 로그가 찍히면 안 된다 — 곧바로 RuntimeError 로 빠진다"
    )


def test_fetch_pull_request_chains_mid_pagination_cause_when_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ABA 페이지 race 가 매 시도마다 반복돼 exhaustion 으로 끝나면, 마지막 ABA
    예외가 RuntimeError 의 `__cause__` 로 chain 되고 메시지에 mode 가 표기돼야 한다.

    회귀 방지 (codex PR #19 review #4): mid-pagination 모드와 fetch 시작-끝 모드는
    디버깅 액션이 다르다 — 전자는 페이지네이션 중간 체크 로직, 후자는 단일 시점 race.
    같은 메시지로 묶이면 운영자가 잘못된 방향으로 진단을 시작할 수 있다.

    시나리오: ABA 시퀀스를 반복해 모든 시도가 mid-pagination 에서 실패하도록 구성.
    """
    # 매 시도가 page=1 받고 post-page 체크에서 다른 SHA 발견 → ABA 발생.
    # 시도 1: pulls=A (initial), files1, files2, pulls=B (post-page) → mismatch → raise
    # 시도 2: pulls=B (initial), files1, files2, pulls=C (post-page) → mismatch → raise
    # 시도 3: pulls=C (initial), files1, files2, pulls=D (post-page) → mismatch → exhausted
    fake, _counters = _build_fake_urlopen_paginated_aba(
        sha_sequence=["A", "B", "B", "C", "C", "D", "D", "E"]
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    with pytest.raises(RuntimeError, match="head_sha kept changing") as exc_info:
        client.fetch_pull_request(RepoRef("o", "r"), 9, installation_id=7)

    # 메시지에 mid-pagination 모드 명시
    assert "mid-pagination" in str(exc_info.value), (
        "ABA exhaustion 케이스에선 메시지에 'mid-pagination' 이 들어가야 운영자가 "
        "fetch 시작-끝 모드와 구분 가능"
    )
    # cause chain — 마지막 ABA 예외가 traceback 에 노출돼야 진단 정확도가 올라감
    cause = exc_info.value.__cause__
    assert cause is not None
    assert "mid-pagination" in str(cause)


def test_fetch_pull_request_reports_start_end_mode_when_last_failure_is_start_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """마지막 시도가 start/end 모드로 끝났는데 이전 시도의 mid-pagination 예외가 남아
    잘못 보고되는 회귀 방지 (codex PR #19 review #6).

    `last_mid_pagination_exc` 변수가 시도 간에 리셋되지 않으면, 시도 1 이 mid-pagination
    으로 실패한 뒤 시도 3 이 start/end mismatch 로 실패해도 옛 mid-pagination 예외가
    남아 있어 exhaustion 메시지가 잘못된 모드를 가리킨다. 시도 시작 시 None 으로 초기화
    해야 한다.

    시나리오 (단일 페이지 PR, post-page + final recheck 둘 다 post-page check 함수):
      - 시도 1: pulls=A (initial), post-page=B (shas[1]) → mid-pagination raise
        → last_mid_pagination_exc 에 시도 1 예외 기록, pr_data=None
      - 시도 2: pulls=B (initial 새로), post-page=B (match), recheck=C (mismatch)
        → start/end raise. 만약 리셋 안 되면 옛 시도 1 예외가 여전히 남음.
      - 시도 3: pulls=C (carried), post-page=C (match), recheck=D (mismatch)
        → start/end raise, exhausted. 마지막 모드는 start/end.
    """
    fake, _counters = _build_fake_urlopen_for_fetch_pr(
        head_shas=["A", "B", "B", "B", "C", "C", "D"]
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    with pytest.raises(RuntimeError, match="head_sha kept changing") as exc_info:
        client.fetch_pull_request(RepoRef("o", "r"), 9, installation_id=7)

    # 마지막 시도는 start/end mode 로 실패 — 메시지가 그 모드여야
    msg = str(exc_info.value)
    assert "fetch start/end SHA mismatch" in msg, (
        f"마지막 실패 모드는 start/end 인데 '{msg}' 는 잘못된 모드로 보고함 — "
        "시도 간 last_mid_pagination_exc 가 리셋되지 않아 옛 예외가 남은 회귀"
    )
    assert "mid-pagination" not in msg, (
        "이전 시도의 mid-pagination 예외가 마지막 모드 진단에 누수되면 안 된다"
    )
    # cause chain 도 없어야 — 마지막 실패가 start/end 라 mid-pagination 예외를 chain 할 이유 없음
    assert exc_info.value.__cause__ is None


def _build_fake_urlopen_paginated_aba(
    sha_sequence: list[str],
    *,
    counters: dict[str, int] | None = None,
):
    """멀티페이지 /files + /pulls SHA 시퀀스를 시뮬하는 urlopen.

    - /files?page=1 → 100개 항목 반환 (페이지 강제)
    - /files?page=2 → 50개 항목 반환 (마지막 페이지)
    - /files?page=3+ → 빈 배열
    - /pulls/{n} → `sha_sequence` 순서대로 head_sha 반환 (마지막 값은 계속 사용)
    """
    if counters is None:
        counters = {"pulls": 0, "files1": 0, "files2": 0}

    def fake_urlopen(
        req: urllib.request.Request,
        *,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> _FakeResponse:
        if "access_tokens" in req.full_url:
            return _FakeResponse(b'{"token": "tkn", "expires_at": ""}')
        if "/files" in req.full_url:
            # `&page=N` 로 매칭 — `per_page=100` 안의 "page=1" 오탐 회피 (URL 에
            # `per_page=100&page=2` 가 들어가면 `"page=1"` substring 검색이
            # 히트해서 무한 루프가 나던 bug 가 있었음).
            if "&page=1" in req.full_url:
                counters["files1"] += 1
                # 100개 — GitHub 페이지 크기 꽉 채워서 다음 페이지 있음을 signaling
                items = [
                    {"filename": f"p1_{i}.py", "patch": "@@ -1 +1 @@\n+x\n"}
                    for i in range(100)
                ]
                return _FakeResponse(json.dumps(items).encode())
            if "&page=2" in req.full_url:
                counters["files2"] += 1
                items = [
                    {"filename": f"p2_{i}.py", "patch": "@@ -1 +1 @@\n+y\n"}
                    for i in range(50)
                ]
                return _FakeResponse(json.dumps(items).encode())
            return _FakeResponse(b'[]')
        # /pulls/{n}
        idx = min(counters["pulls"], len(sha_sequence) - 1)
        sha = sha_sequence[idx]
        counters["pulls"] += 1
        return _FakeResponse(json.dumps({
            "title": "t",
            "body": "b",
            "draft": False,
            "head": {"sha": sha, "ref": "feat", "repo": {"clone_url": "https://x.git"}},
            "base": {"sha": "base-sha", "ref": "main"},
        }).encode())

    return fake_urlopen, counters


def test_fetch_pull_request_detects_aba_race_mid_pagination(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ABA race: /files 페이지1 과 페이지2 사이 head 가 A→B 로 변했다가 최종 recheck 시
    다시 A 로 돌아온 경우. 시작/끝 SHA 비교만으로는 못 잡는 경계 조건을 잠금.

    회귀 방지 (codex PR #19 review #3): 페이지 1 은 SHA A 기준 patch, 페이지 2 는 SHA B
    기준 patch 로 섞여서 들어왔는데, PullRequest 엔 A 의 head_sha + 혼합 addable_lines
    가 박혀 인라인 코멘트 위치가 어긋난다. 페이지 사이 /pulls 체크로 즉시 감지 후 전체
    fetch 를 재시도해야 한다.

    시나리오 (시도 1):
      - /pulls (initial) → A
      - /files page=1 (100개, A 기준)
      - /pulls (between-pages) → B   ← 여기서 감지 → _HeadShaChangedMidFetch 발생
    시나리오 (시도 2, 재시작):
      - /pulls (initial) → B (안정)
      - /files page=1 (100개, B 기준)
      - /pulls (between-pages) → B (변동 없음)
      - /files page=2 (50개, B 기준)
      - /pulls (final recheck) → B (일관) → 정상 반환
    """
    # ABA: initial A, mid-pagination A→B, 그 뒤로는 계속 B
    # 그러면 between-pages 체크에서 B 를 발견하고 재시도 → 2차는 모두 B 로 성공
    fake, counters = _build_fake_urlopen_paginated_aba(
        sha_sequence=["A", "B", "B", "B", "B", "B"]
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    with caplog.at_level(logging.WARNING):
        pr = client.fetch_pull_request(RepoRef("o", "r"), 9, installation_id=7)

    # 2차 시도에서 모두 B 였으므로 최종 head_sha 는 B
    assert pr.head_sha == "B"
    # mid-pagination 에서 race 감지됐다는 WARN 관측
    mid_warns = [
        r for r in caplog.records if "mid-pagination" in r.getMessage()
    ]
    assert len(mid_warns) == 1, "페이지 사이 SHA 변동이 감지돼 WARN 한 줄 남아야 한다"
    assert "A" in mid_warns[0].getMessage() and "B" in mid_warns[0].getMessage()
    # changed_files 는 2차 시도의 150개 (100 + 50)
    assert len(pr.changed_files) == 150


def test_fetch_pull_request_detects_single_page_aba(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """단일 페이지 PR 의 ABA race: `initial=A → /files=B → recheck=A` 를 post-page check 로 잡는다.

    회귀 방지 (codex PR #19 review #7): 이전엔 `page > 1` 에서만 post-page check 를
    수행해 단일 페이지 PR 의 ABA race (바깥 start/end 비교만으로는 못 잡음) 를 놓쳤다.
    첫 페이지도 검증 대상이라는 정책의 정합성.

    시나리오: initial=A, page=1 받음 (A 기준이든 B 기준이든 상관없음), post-page check
    에서 SHA=B 발견 → mid-pagination raise. 재시도 후 2차에서는 SHA 가 안정돼 정상 반환.
    """
    # 시도 1: init=A, post-page=B → mid-pagination raise
    # 시도 2: init=A (새로, pr_data=None 이었음), post-page=A, recheck=A → return
    fake, counters = _build_fake_urlopen_for_fetch_pr(
        head_shas=["A", "B", "A", "A", "A"]
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    pr = client.fetch_pull_request(RepoRef("o", "r"), 9, installation_id=7)

    assert pr.head_sha == "A", "재시도 후 안정된 A 로 반환"
    # ABA 가 post-page 에서 감지됐는지 확인: /pulls 5회 (1차 2회 + 2차 initial+post-page+recheck=3회)
    assert counters["pulls"] == 5
    assert counters["files"] == 2


def test_fetch_pull_request_skips_check_on_empty_final_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """정확히 100 배수 파일을 가진 PR 에서 빈 마지막 페이지는 post-page check 를 스킵.

    회귀 방지 (codex PR #19 review #8): `if not files: break` 를 SHA check 보다 먼저
    두면 누적할 게 없는 빈 페이지에 대해 불필요한 /pulls 호출이 발생하지 않는다. 이
    최적화가 깨지면 정확히 100 배수 파일 PR 의 API 비용이 늘고, 빈 페이지에서 head_sha
    가 움직였다 하더라도 누적된 데이터는 이전 페이지 기준이라 사실상 오탐.

    시나리오: page=1 에서 100개 반환 → page=2 에서 빈 배열. page=2 응답 직후에 empty
    check 가 먼저 발동해 loop 이 break, post-page check 는 일어나지 않아야.
    """
    counters: dict[str, int] = {"pulls": 0, "files": 0}

    def fake_urlopen(
        req: urllib.request.Request,
        *,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> _FakeResponse:
        if "access_tokens" in req.full_url:
            return _FakeResponse(b'{"token": "tkn", "expires_at": ""}')
        if "/files" in req.full_url:
            counters["files"] += 1
            if "&page=1" in req.full_url:
                items = [{"filename": f"p_{i}.py", "patch": "@@ -1 +1 @@\n+x\n"} for i in range(100)]
                return _FakeResponse(json.dumps(items).encode())
            return _FakeResponse(b'[]')  # page=2 는 빈 배열
        counters["pulls"] += 1
        return _FakeResponse(json.dumps({
            "title": "t", "body": "b", "draft": False,
            "head": {"sha": "A", "ref": "feat", "repo": {"clone_url": "https://x.git"}},
            "base": {"sha": "base", "ref": "main"},
        }).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    pr = client.fetch_pull_request(RepoRef("o", "r"), 9, installation_id=7)

    assert pr.head_sha == "A"
    assert len(pr.changed_files) == 100
    # /pulls 호출 = initial + post-page-1 + (no check on empty page=2) + final recheck = 3
    # 만약 empty check 가 없었다면 4 회 (page=2 의 post-page check 포함).
    assert counters["pulls"] == 3, (
        "빈 페이지에서는 post-page check 를 스킵해야 정확히 100 배수 파일 PR 의 비용 낭비 방지"
    )
    assert counters["files"] == 2  # page=1 + page=2 (empty)


# --- 삭제된 fork PR fallback (merged from PR #21) ----------------------------


def _make_fake_urlopen_for_pr_meta(head_repo: Any) -> Any:
    """_resolve_fetch_source 테스트용 urlopen — head.repo 값을 자유롭게 제어.

    `head_repo` 를 `None` 또는 빈 dict 또는 정상 dict 로 넘겨서 fork 시나리오를 시뮬.
    """

    def fake_urlopen(
        req: urllib.request.Request,
        *,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> _FakeResponse:
        if "access_tokens" in req.full_url:
            return _FakeResponse(b'{"token": "tkn", "expires_at": ""}')
        if "/files" in req.full_url:
            return _FakeResponse(b'[]')
        return _FakeResponse(json.dumps({
            "title": "t",
            "body": "b",
            "draft": False,
            "head": {"sha": "abc", "ref": "feat", "repo": head_repo},
            "base": {"sha": "def", "ref": "main", "repo": {"clone_url": "https://base/x.git"}},
        }).encode())

    return fake_urlopen


def test_fetch_pull_request_falls_back_to_base_repo_when_head_fork_deleted(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """삭제된 fork 시나리오: `head.repo == null` 응답에서도 TypeError 없이 살아남는다.

    실 GitHub API 동작: 사용자가 fork PR 을 제출한 뒤 그 fork 를 삭제하면 다음 `/pulls/{n}`
    응답의 `head.repo` 가 명시적 `null` 로 온다. 이전 구현은 `head["repo"]["clone_url"]`
    을 직접 인덱싱해 `TypeError: 'NoneType' object is not subscriptable` 로 fetch 가 통째로
    실패하고 PR 한 건이 유실됐다.

    회귀 방지: base.repo.clone_url 로 fallback, WARN 로그 1건으로 운영 관측 가능.
    """
    monkeypatch.setattr(urllib.request, "urlopen", _make_fake_urlopen_for_pr_meta(None))
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    with caplog.at_level(logging.WARNING):
        pr = client.fetch_pull_request(RepoRef("o", "r"), 9, installation_id=7)

    # fallback URL 이 박혀야 (head URL 에는 접근 불가능했으므로)
    assert pr.clone_url == "https://base/x.git"
    # fetch_ref 도 함께 전환돼야 — base repo 에서는 head_sha 직접 fetch 가 막힐 수 있고
    # PR ref 만 PR 스냅샷에 도달 가능 (codex PR #21 review #1).
    assert pr.fetch_ref == "refs/pull/9/head", (
        "fork 삭제 fallback 시 fetch_ref 도 PR ref 로 전환돼야 GitRepoFetcher 가 "
        "base repo 에서 PR 스냅샷을 받을 수 있다"
    )
    # 운영 관측 WARN — "fork 삭제된 PR" 빈도 추적 가능, ref 도 메시지에 포함
    warns = [r for r in caplog.records if "deleted fork" in r.getMessage()]
    assert len(warns) == 1, "fallback 발생 시 WARN 한 줄이 남아야 한다"
    assert "o/r" in warns[0].getMessage() and "#9" in warns[0].getMessage()
    assert "refs/pull/9/head" in warns[0].getMessage()


def test_fetch_pull_request_falls_back_when_head_repo_missing_clone_url(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """head.repo 가 dict 로는 존재하지만 clone_url 키가 없는 비정상 응답도 방어.

    GitHub API 의 공식 스키마상 드물지만, 부분 응답·미래 스키마 변경 등 방어적 처리.
    `head.repo` dict 에 clone_url 키가 없거나 빈 문자열이면 fallback.
    """
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _make_fake_urlopen_for_pr_meta({"owner": "user"}),  # clone_url 키 없음
    )
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    with caplog.at_level(logging.WARNING):
        pr = client.fetch_pull_request(RepoRef("o", "r"), 9, installation_id=7)

    assert pr.clone_url == "https://base/x.git"
    assert pr.fetch_ref == "refs/pull/9/head"
    assert any("deleted fork" in r.getMessage() for r in caplog.records)


def test_fetch_pull_request_uses_head_clone_url_in_normal_case(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """정상 케이스: head.repo 존재 → head.repo.clone_url 사용, WARN 없음.

    회귀 방지: fallback 규칙이 너무 넓게 발동해서 정상 PR 의 head URL 까지 덮어쓰면
    fork 의 head-only commit 을 클론하지 못하는 부작용이 생긴다. 정상 응답에선 head
    URL 이 그대로 박혀야 하고, "deleted fork" WARN 도 나오면 안 된다.
    """
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _make_fake_urlopen_for_pr_meta({"clone_url": "https://fork/x.git"}),
    )
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    with caplog.at_level(logging.WARNING):
        pr = client.fetch_pull_request(RepoRef("o", "r"), 9, installation_id=7)

    assert pr.clone_url == "https://fork/x.git", "정상 케이스엔 head URL 이 박혀야"
    # 정상 케이스: fetch_ref == head_sha (PR ref 로 전환되면 안 됨)
    assert pr.fetch_ref == pr.head_sha, "정상 케이스엔 fetch_ref 가 head_sha 와 동일해야"
    deleted_fork_warns = [r for r in caplog.records if "deleted fork" in r.getMessage()]
    assert deleted_fork_warns == [], "정상 케이스엔 fallback WARN 이 없어야 한다"


def test_http_attaches_response_detail_to_httperror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_http` 가 422 등 에러 응답 본문을 exc 에 첨부 — 호출부의 분기 정확도 향상.

    `exc.read()` 는 1회용 stream 이라 _http 에서 한 번 읽고 나면 호출부가 다시 못 읽음.
    `gemini_review_detail` 커스텀 속성으로 첨부해 두면, 향후 retry 로직이 "line must be
    part of the diff" 같은 구체 사유로 분기를 좁힐 수 있다.
    """
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_a, **_k: (_ for _ in ()).throw(
            urllib.error.HTTPError(
                "https://api.github.com/x",
                422,
                "Unprocessable Entity",
                {},  # type: ignore[arg-type]
                io.BytesIO(b'{"message": "Validation Failed", "errors": ["foo"]}'),
            )
        ),
    )

    client = GitHubAppClient(app_id=1, private_key_pem="-")

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        client._http("GET", "https://api.github.com/x", auth="token t")

    detail = getattr(exc_info.value, "gemini_review_detail", None)
    assert detail is not None, "HTTPError 에 응답 본문이 첨부돼야 한다"
    assert "Validation Failed" in detail
    assert "foo" in detail


# --- list_self_review_comments (Layer D 의 history grounding 데이터 소스) ---


def _comments_response(items: list[dict[str, Any]]) -> bytes:
    return json.dumps(items).encode()


def _make_fake_comments_urlopen(pages: list[list[dict[str, Any]]]):
    """`/pulls/{n}/comments?page=K` 흐름을 시뮬레이션하는 urlopen 대체.

    - access_tokens: 정상 토큰 응답
    - /pulls/{n}/comments?page=K: pages[K-1] 반환 (없으면 빈 배열)
    """
    seen_pages: list[int] = []

    def fake_urlopen(
        req: urllib.request.Request,
        *,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> _FakeResponse:
        if "access_tokens" in req.full_url:
            return _FakeResponse(b'{"token": "tkn", "expires_at": ""}')
        # parse page= from query string
        page_str = req.full_url.split("page=")[-1]
        page = int(page_str)
        seen_pages.append(page)
        if page <= len(pages):
            return _FakeResponse(_comments_response(pages[page - 1]))
        return _FakeResponse(_comments_response([]))

    return fake_urlopen, seen_pages


def test_list_self_review_comments_filters_by_app_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """본 App 의 코멘트만 통과 — 사람·다른 봇 제외.

    회귀 방지: dedup 의 의미는 "본 봇이 무시되는 신호". `user.login` 기반 필터는 봇
    이름 변경에 깨지므로 `performed_via_github_app.id` 를 1차 신호로 사용. 이 테스트는
    OUR_APP_ID 와 다른 ID, 사람 코멘트, performed_via_github_app 누락 (사람 코멘트의
    표준 형태) 을 모두 제외함을 lock.
    """
    OUR_APP_ID = 1234
    OTHER_APP_ID = 9999
    # id/commit_id 는 PR #28 (Layer E) 에서 PostedReviewComment 의 필수 필드로 추가됨
    # — reply API + diff 비교에 사용. fake response 도 production 응답 구조와 일치시켜야.
    pages = [
        [
            {
                "id": 1001,
                "commit_id": "abc1234",
                "path": "a.py",
                "line": 10,
                "body": "[Major] 본 봇이 게시.",
                "performed_via_github_app": {"id": OUR_APP_ID, "name": "gemini-pr-review-bot"},
            },
            {
                "id": 1002,
                "commit_id": "abc1234",
                "path": "a.py",
                "line": 20,
                "body": "[Critical] 다른 봇 (codex) 의 코멘트.",
                "performed_via_github_app": {"id": OTHER_APP_ID, "name": "codex-review-bot"},
            },
            {
                "id": 1003,
                "commit_id": "abc1234",
                "path": "a.py",
                "line": 30,
                "body": "사람이 단 코멘트.",
                # performed_via_github_app 키 자체 누락 = 사람 코멘트의 일반적 형태
            },
        ]
    ]
    fake, _ = _make_fake_comments_urlopen(pages)
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=OUR_APP_ID, private_key_pem="-")
    out = client.list_self_review_comments(_sample_pr())

    assert len(out) == 1, "본 봇 (OUR_APP_ID) 의 코멘트만 살아남아야"
    assert out[0].path == "a.py" and out[0].line == 10
    assert out[0].body.startswith("[Major]")


def test_list_self_review_comments_skips_outdated_with_null_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """force-push 후 anchor 깨진 outdated 코멘트 (`line: null`) 는 드롭.

    회귀 방지: line=null 인 코멘트는 dedup key 형성 불가. original_line 으로 fallback
    하면 유저가 의도적으로 코드를 옮긴 경우 가짜 dedup 발동 — 보수적으로 제외.
    """
    OUR_APP_ID = 1234
    pages = [
        [
            {
                "id": 2001,
                "commit_id": "abc1234",
                "path": "a.py",
                "line": None,  # outdated
                "original_line": 10,  # GitHub 가 보존하지만 우리는 안 씀
                "body": "[Major] outdated.",
                "performed_via_github_app": {"id": OUR_APP_ID},
            },
            {
                "id": 2002,
                "commit_id": "abc1234",
                "path": "a.py",
                "line": 25,
                "body": "[Major] 살아 있는 코멘트.",
                "performed_via_github_app": {"id": OUR_APP_ID},
            },
        ]
    ]
    fake, _ = _make_fake_comments_urlopen(pages)
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=OUR_APP_ID, private_key_pem="-")
    out = client.list_self_review_comments(_sample_pr())

    assert len(out) == 1, "line=null 은 드롭, line=int 만 통과"
    assert out[0].line == 25


def test_list_self_review_comments_paginates_until_short_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """100 + 50 응답 → 두 페이지 fetch, 50건 페이지에서 종료.

    회귀 방지: 단일 페이지 가정으로 코드를 단순화하면 큰 PR 에서 오래된 코멘트가
    history 에서 누락돼 dedup 미발동. _fetch_files_for_pr 와 동일 패턴 사용.
    """
    OUR_APP_ID = 1234
    page1 = [
        {
            "id": 3000 + i,
            "commit_id": "abc1234",
            "path": "a.py",
            "line": i,
            "body": f"[Major] page1 #{i}.",
            "performed_via_github_app": {"id": OUR_APP_ID},
        }
        for i in range(1, 101)
    ]
    page2 = [
        {
            "id": 4000 + i,
            "commit_id": "abc1234",
            "path": "b.py",
            "line": i,
            "body": f"[Major] page2 #{i}.",
            "performed_via_github_app": {"id": OUR_APP_ID},
        }
        for i in range(1, 51)
    ]
    fake, seen_pages = _make_fake_comments_urlopen([page1, page2])
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=OUR_APP_ID, private_key_pem="-")
    out = client.list_self_review_comments(_sample_pr())

    assert seen_pages == [1, 2], "100건 첫 페이지 → 2페이지 추가 fetch, 50건 → 종료"
    assert len(out) == 150, "두 페이지 합쳐 150건"


def test_list_self_review_comments_uses_newest_first_sort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/pulls/{n}/comments` 호출 URL 에 `sort=created&direction=desc` 가 포함돼야 한다.

    회귀 방지 (codex PR #25 review #2): GitHub 기본 정렬은 `created` ASC (오래된 순).
    그대로 두면 1000+ 코멘트가 쌓인 장기 PR 의 최신 코멘트가 10-page cap 밖으로 밀려나
    dedup 무력화 — Layer D 의 주된 dedup 대상인 직전 push 코멘트도 못 잡는 회귀.

    `direction=desc` 보장이 깨지면 cap 안에서 최신 코멘트가 누락될 수 있으므로 URL 자체에
    파라미터가 반드시 들어가야 한다는 invariant 를 lock.
    """
    OUR_APP_ID = 1234
    captured_urls: list[str] = []

    def fake_urlopen(
        req: urllib.request.Request,
        *,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> _FakeResponse:
        if "access_tokens" in req.full_url:
            return _FakeResponse(b'{"token": "tkn", "expires_at": ""}')
        captured_urls.append(req.full_url)
        return _FakeResponse(b"[]")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=OUR_APP_ID, private_key_pem="-")
    client.list_self_review_comments(_sample_pr())

    assert len(captured_urls) >= 1, "코멘트 endpoint 가 호출돼야"
    url = captured_urls[0]
    assert "sort=created" in url, (
        f"sort=created 가 빠지면 GitHub 기본 ASC 정렬로 cap 밖 누락. URL: {url}"
    )
    assert "direction=desc" in url, (
        f"direction=desc 가 빠지면 최신 코멘트 우선 보장 깨짐. URL: {url}"
    )


# --- reply_to_review_comment (Layer E follow-up) -----------------------------


def test_reply_to_review_comment_posts_to_thread_replies_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """대댓글 게시 URL 이 `/pulls/{n}/comments/{cid}/replies` 형식이어야 한다.

    회귀 방지: GitHub 의 reply API 경로는 일반 issue comment endpoint
    (`/issues/{n}/comments`) 와 다름. 잘못된 endpoint 로 보내면 대댓글 thread 가
    형성되지 않고 별도 코멘트로 게시됨.
    """
    captured: dict[str, Any] = {}

    def fake_urlopen(
        req: urllib.request.Request,
        *,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> _FakeResponse:
        if "access_tokens" in req.full_url:
            return _FakeResponse(b'{"token": "tkn", "expires_at": ""}')
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8")) if req.data else None
        return _FakeResponse(b'{"id": 9999}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    client.reply_to_review_comment(_sample_pr(), comment_id=1234, body="follow-up 본문")

    assert captured["url"].endswith("/pulls/9/comments/1234/replies"), (
        f"reply endpoint 가 thread replies 경로여야 함. 실제: {captured['url']}"
    )
    assert captured["body"] == {"body": "follow-up 본문"}


def test_reply_to_review_comment_skips_post_in_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DRY_RUN 모드에선 reply 호출이 게시 없이 로그만 남겨야 한다 (post_review/post_comment 와 동일 패턴)."""
    posted: list[str] = []

    def fake_urlopen(
        req: urllib.request.Request,
        *,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> _FakeResponse:
        if "access_tokens" in req.full_url:
            return _FakeResponse(b'{"token": "tkn", "expires_at": ""}')
        posted.append(req.full_url)
        return _FakeResponse(b'{}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-", dry_run=True)
    client.reply_to_review_comment(_sample_pr(), comment_id=1, body="x")

    assert posted == [], "DRY_RUN 이면 어떤 게시도 일어나지 않아야"
