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

    # /pulls/{n} 를 두 번 짚어야 (앞: head_sha 확보, 뒤: 일관성 재확인)
    assert counters["pulls"] == 2, "head_sha 일관성 재확인 호출이 누락됐다"
    assert counters["files"] == 1, "정상 케이스에선 /files 가 한 번만 호출돼야 한다"
    assert pr.head_sha == "abc"


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
    """`/pulls/{n}` 과 `/files` 사이 head_sha 가 바뀌면 한 번 재시도해 일관된 스냅샷을 얻어야 한다.

    회귀 방지: 재시도 로직이 빠지면 첫 시도의 어긋난 데이터를 그대로 PullRequest 에
    박아 후속 인라인 분할이 깨진다. 두 번째 시도에서 head_sha 가 안정되면 그 SHA 의
    일관된 스냅샷으로 정상 반환해야 한다.

    호출 절감 검증도 포함: 1차의 recheck 결과를 2차의 시작점으로 재사용하므로 /pulls
    호출은 (1차 initial + 1차 recheck + 2차 recheck) = 3 회. 4 회로 보이면 재사용
    최적화가 회귀.
    """
    # 1차: pulls=abc → files → pulls=def (변경 감지) ⇒ 재시도, recheck=def 를 carry
    # 2차: (carried pr_data, sha=def) → files → pulls=def (안정) ⇒ 확정
    fake, counters = _build_fake_urlopen_for_fetch_pr(
        head_shas=["abc", "def", "def", "def"]
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    with caplog.at_level(logging.WARNING):
        pr = client.fetch_pull_request(RepoRef("o", "r"), 9, installation_id=7)

    # /pulls 3번 (initial 1 + recheck 1 + 2차 recheck 1) — recheck 재사용으로 4→3
    assert counters["pulls"] == 3
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
    """매 시도마다 head_sha 가 바뀌면(force-push 폭주) 무한 retry 대신 명시적 실패.

    조용히 잘못된 데이터로 진행하는 것이 가장 위험한 실패 모드 — 차라리 webhook 큐
    수준에서 빼버리고 다음 push 의 새 webhook 이 새 시작점이 되도록 하는 게 안전.

    회귀 방지: 마지막 시도에서 "retrying" 로그가 더 안 찍히는지도 함께 확인. 안 그러면
    "retry 한다고 했는데 곧바로 실패" 라는 이상한 로그 흐름이 운영자를 혼란시킨다
    (gemini PR #19 review #2). _MAX_FETCH_ATTEMPTS=3 이면 retry 로그는 최대 2건.
    """
    # 매번 다른 SHA 를 돌려주는 시퀀스 — 어떤 시도도 안정되지 않음
    fake, _counters = _build_fake_urlopen_for_fetch_pr(
        head_shas=["s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9", "s10"]
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError, match="head_sha kept changing"):
            client.fetch_pull_request(RepoRef("o", "r"), 9, installation_id=7)

    # _MAX_FETCH_ATTEMPTS=3 → 1차·2차 mismatch 시 "retrying" 로그, 3차는 곧바로 raise.
    # retry 로그는 정확히 2건 (3차에서 거짓 retry 메시지가 찍히면 회귀).
    retry_warns = [r for r in caplog.records if "retrying attempt" in r.getMessage()]
    assert len(retry_warns) == 2, (
        "마지막 시도에선 retry 로그가 찍히면 안 된다 — 곧바로 RuntimeError 로 빠진다"
    )


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
