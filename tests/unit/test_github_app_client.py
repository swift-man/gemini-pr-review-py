import io
import json
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
    """Intercept urlopen + jwt.encode so we can inspect GitHubAppClient wiring
    without making real network calls or needing a real RSA key."""
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


def _sample_pr() -> PullRequest:
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
    )


def test_post_review_drops_inline_comments_and_retries_on_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """모델이 diff 범위 밖 라인을 지적해 422 가 나면 comments 를 비우고 재시도한다.

    Reviews API 는 bulk 등록이라 inline comment 하나가 잘못된 라인을 가리키면 전체
    등록이 거부된다. 어느 comment 가 문제인지 API 가 구분해서 알려주지 않으므로,
    본문(요약 / 좋은 점 / 개선할 점) 만이라도 PR 에 남기기 위해 comments 를 비우고
    1회 재시도하는 정책을 고정한다.
    """
    posted_bodies: list[dict[str, Any]] = []

    def fake_urlopen(
        req: urllib.request.Request,
        *,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> _FakeResponse:
        # installation token 호출은 정상 응답으로 처리
        if "access_tokens" in req.full_url:
            return _FakeResponse(b'{"token": "tkn", "expires_at": ""}')

        # review POST 호출만 캡처 — 첫 번째는 422, 두 번째는 성공
        assert req.data is not None
        body = json.loads(req.data.decode("utf-8"))
        posted_bodies.append(body)
        if len(posted_bodies) == 1:
            raise urllib.error.HTTPError(
                req.full_url,
                422,
                "Unprocessable Entity",
                {},  # type: ignore[arg-type]
                io.BytesIO(
                    b'{"message": "Validation Failed", "errors": ['
                    b'{"resource": "PullRequestReviewComment", '
                    b'"code": "custom", '
                    b'"message": "pull_request_review_thread.line must be part of the diff"}]}'
                ),
            )
        return _FakeResponse(b'{"id": 1}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.COMMENT,
        positives=("좋음",),
        improvements=("개선",),
        findings=(Finding(path="a.py", line=42, body="diff 범위 밖 라인"),),
    )

    # 예외가 삼켜져야 함 (본문만이라도 게시)
    client.post_review(_sample_pr(), result)

    assert len(posted_bodies) == 2, "초기 POST + 재시도 = 2회 호출돼야 함"

    # 1차: comments 포함
    assert len(posted_bodies[0]["comments"]) == 1
    assert posted_bodies[0]["comments"][0]["path"] == "a.py"
    assert posted_bodies[0]["comments"][0]["line"] == 42

    # 2차: comments 비워졌지만 body 와 기타 필드는 동일 (본문 보존)
    assert posted_bodies[1]["comments"] == []
    assert posted_bodies[1]["body"] == posted_bodies[0]["body"]
    assert posted_bodies[1]["commit_id"] == posted_bodies[0]["commit_id"]
    assert posted_bodies[1]["event"] == posted_bodies[0]["event"]


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

    with pytest.raises(urllib.error.HTTPError) as exc:
        client.post_review(_sample_pr(), result)

    assert exc.value.code == 500
    assert call_count == 1
