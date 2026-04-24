import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from gemini_review.domain import FileDump, PullRequest, RepoRef
from gemini_review.infrastructure.gemini_cli_engine import (
    _is_retryable_model_failure,
    GeminiAuthError,
    GeminiCliEngine,
)


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _engine(creds: Path) -> GeminiCliEngine:
    return GeminiCliEngine(binary="gemini", model="gemini-2.5-pro", oauth_creds_path=creds)


def _write_good_creds(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"refresh_token": "abc", "access_token": "xyz", "token_uri": "..."}),
        encoding="utf-8",
    )


def _sample_pr() -> PullRequest:
    return PullRequest(
        repo=RepoRef("o", "r"),
        number=1,
        title="title",
        body="",
        head_sha="abc",
        head_ref="feature",
        base_sha="def",
        base_ref="main",
        clone_url="https://example.com/o/r.git",
        changed_files=("src/a.py",),
        installation_id=7,
        is_draft=False,
    )


def test_verify_auth_passes_with_binary_and_creds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creds = tmp_path / "oauth_creds.json"
    _write_good_creds(creds)

    def fake_run(*_args: Any, **_kwargs: Any) -> _FakeCompleted:
        return _FakeCompleted(0, "0.1.11\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    status = _engine(creds).verify_auth()
    assert status.startswith("gemini ")
    assert "oauth_creds.json" in status


def test_verify_auth_raises_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creds = tmp_path / "oauth_creds.json"
    _write_good_creds(creds)

    def fake_run(*_args: Any, **_kwargs: Any) -> _FakeCompleted:
        raise FileNotFoundError("gemini: not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(GeminiAuthError) as exc:
        _engine(creds).verify_auth()
    assert "GEMINI_BIN" in str(exc.value)


def test_verify_auth_raises_on_binary_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creds = tmp_path / "oauth_creds.json"
    _write_good_creds(creds)

    def fake_run(*_args: Any, **_kwargs: Any) -> _FakeCompleted:
        raise subprocess.TimeoutExpired(cmd="gemini", timeout=10)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(GeminiAuthError) as exc:
        _engine(creds).verify_auth()
    assert "10초" in str(exc.value)


def test_verify_auth_raises_when_creds_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 바이너리 프로브는 통과 — 오직 creds 파일 부재로만 실패하도록 구성.
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted(0, "0.1.11\n"))
    missing = tmp_path / "does_not_exist.json"
    with pytest.raises(GeminiAuthError) as exc:
        _engine(missing).verify_auth()
    assert "로그인" in str(exc.value)
    assert str(missing) in str(exc.value)


def test_verify_auth_raises_when_creds_corrupt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted(0, "0.1.11\n"))
    creds = tmp_path / "oauth_creds.json"
    creds.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(GeminiAuthError) as exc:
        _engine(creds).verify_auth()
    assert "읽지 못했습니다" in str(exc.value)


def test_verify_auth_raises_when_refresh_token_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted(0, "0.1.11\n"))
    creds = tmp_path / "oauth_creds.json"
    creds.write_text(json.dumps({"access_token": "xyz"}), encoding="utf-8")
    with pytest.raises(GeminiAuthError) as exc:
        _engine(creds).verify_auth()
    assert "refresh_token" in str(exc.value)


def test_verify_auth_raises_on_binary_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creds = tmp_path / "oauth_creds.json"
    _write_good_creds(creds)

    def fake_run(*_args: Any, **_kwargs: Any) -> _FakeCompleted:
        return _FakeCompleted(1, "", "boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(GeminiAuthError) as exc:
        _engine(creds).verify_auth()
    assert "실행에 실패" in str(exc.value)


def test_review_invokes_prompt_mode_with_stdin_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeCompleted:
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return _FakeCompleted(
            0,
            '{"summary": "ok", "event": "COMMENT", "comments": []}',
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GeminiCliEngine(binary="gemini", model="gemini-2.5-pro").review(
        _sample_pr(),
        FileDump(entries=(), total_chars=0),
    )

    assert captured["cmd"] == ["gemini", "-m", "gemini-2.5-pro", "-p", " "]
    assert "=== PR METADATA ===" in str(captured["input"])
    assert result.summary == "ok"
    # primary 모델이 그대로 성공한 경우 그 이름이 결과에 주입돼야 한다.
    assert result.model == "gemini-2.5-pro"


def test_review_falls_back_when_preview_model_capacity_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        calls.append(cmd)
        if len(calls) == 1:
            return _FakeCompleted(
                1,
                stderr=(
                    "429 RESOURCE_EXHAUSTED: "
                    "No capacity available for model gemini-3.1-pro-preview"
                ),
            )
        return _FakeCompleted(
            0,
            '{"summary": "fallback ok", "event": "COMMENT", "comments": []}',
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GeminiCliEngine(
        binary="gemini",
        model="gemini-3.1-pro-preview",
        fallback_models=("gemini-2.5-pro",),
    ).review(_sample_pr(), FileDump(entries=(), total_chars=0))

    assert [cmd[2] for cmd in calls] == [
        "gemini-3.1-pro-preview",
        "gemini-2.5-pro",
    ]
    assert result.summary == "fallback ok"
    # fallback 이 발동했으므로 primary 가 아닌 실제 응답을 만든 모델이 결과에 담겨야 한다.
    assert result.model == "gemini-2.5-pro"


def test_review_falls_back_when_primary_returns_empty_stdout(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """gemini 가 returncode=0 + 빈 stdout 으로 응답하면 fallback 모델로 넘어가야 한다.

    실관측 회귀 (mlx-pr-review-py#31, 2026-04): gemini-3.1-pro-preview 가 일부 PR 에
    대해 성공 종료 + 완전 빈 stdout 으로 응답. 이전 코드는 그대로 파서에 넘겨 빈
    ReviewResult 가 만들어지고 "Gemini 응답을 파싱하지 못했습니다." 라는 무의미한
    리뷰가 GitHub 에 게시. fallback 체인이 발동하지 않는 회귀.

    수정 후: 빈 stdout 도 retryable 로 간주해 다음 모델로 넘긴다.
    """
    import logging

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        calls.append(cmd)
        if len(calls) == 1:
            # 1차: returncode=0 이지만 stdout 완전 빈 응답
            return _FakeCompleted(0, stdout="", stderr="")
        # 2차 (fallback): 정상 JSON
        return _FakeCompleted(
            0,
            '{"summary": "fallback responded", "event": "COMMENT", "comments": []}',
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with caplog.at_level(logging.WARNING):
        result = GeminiCliEngine(
            binary="gemini",
            model="gemini-3.1-pro-preview",
            fallback_models=("gemini-2.5-pro",),
        ).review(_sample_pr(), FileDump(entries=(), total_chars=0))

    # fallback 모델로 넘어가 정상 응답을 받았는지
    assert [cmd[2] for cmd in calls] == [
        "gemini-3.1-pro-preview",
        "gemini-2.5-pro",
    ]
    assert result.summary == "fallback responded"
    assert result.model == "gemini-2.5-pro"
    # 운영 관측: 빈 stdout 발생을 WARN 로 기록해야 빈도 추적 가능
    empty_warns = [
        r for r in caplog.records if "empty stdout" in r.getMessage()
    ]
    assert len(empty_warns) == 1
    assert "gemini-3.1-pro-preview" in empty_warns[0].getMessage()


def test_review_raises_when_all_models_return_empty_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """모든 모델이 빈 stdout 으로 응답하면 RuntimeError raise — 빈 리뷰 게시 차단.

    회귀 방지:
    - 이전엔 빈 stdout 도 "성공" 으로 간주해 빈 ReviewResult 가 GitHub 에 게시됐다.
      exhaustion 시점엔 더이상 게시할 콘텐츠가 없으므로 명시적으로 실패시켜 상위
      핸들러가 "리뷰 생성 실패" 알림으로 처리할 수 있게 한다.
    - 진단 보강 (codex PR #24 review #3): 메시지에 마지막 모델명 + stderr preview 가
      들어가야 운영자가 어떤 모델이 마지막에 실패했는지 즉시 확인 가능.
    """
    def fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        # 모델별로 다른 stderr 를 줘서 마지막 모델의 stderr 가 예외 메시지에 들어가는지 확인
        model_arg = cmd[2] if len(cmd) > 2 else "?"
        return _FakeCompleted(
            0, stdout="", stderr=f"[{model_arg}] hit token cap"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="empty stdout on all") as exc_info:
        GeminiCliEngine(
            binary="gemini",
            model="gemini-3.1-pro-preview",
            fallback_models=("gemini-2.5-pro",),
        ).review(_sample_pr(), FileDump(entries=(), total_chars=0))

    msg = str(exc_info.value)
    # 마지막 모델명이 메시지에 포함돼야 운영 진단 가능
    assert "last_model=gemini-2.5-pro" in msg
    # 마지막 모델의 stderr preview 가 포함돼야 — 빈 응답의 부가 원인 즉시 확인
    assert "hit token cap" in msg
    assert "gemini-2.5-pro" in msg  # stderr 의 모델명도 함께


def test_review_does_not_fall_back_on_non_retryable_cli_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        calls.append(cmd)
        return _FakeCompleted(1, stderr="OAuth login required")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="OAuth login required"):
        GeminiCliEngine(
            binary="gemini",
            model="gemini-3.1-pro-preview",
            fallback_models=("gemini-2.5-pro",),
        ).review(_sample_pr(), FileDump(entries=(), total_chars=0))

    assert [cmd[2] for cmd in calls] == ["gemini-3.1-pro-preview"]


def test_review_falls_back_on_premature_stream_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """실관측 에러(`ERR_STREAM_PREMATURE_CLOSE`) 에서 fallback 모델로 넘어가는지 고정한다.

    Gemini CLI 가 preview 모델 응답 스트림 도중 끊길 때 내는 에러. 모델/서버 쪽 일시
    불안정이라 같은 모델 재시도보다 안정 모델로 폴백하는 편이 실효 있다.
    """
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        calls.append(cmd)
        if len(calls) == 1:
            return _FakeCompleted(
                1,
                stderr=(
                    "Error when talking to Gemini API\n"
                    "Error: Premature close\n"
                    "code: 'ERR_STREAM_PREMATURE_CLOSE'"
                ),
            )
        return _FakeCompleted(
            0,
            '{"summary": "recovered via fallback", "event": "COMMENT", "comments": []}',
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GeminiCliEngine(
        binary="gemini",
        model="gemini-3.1-pro-preview",
        fallback_models=("gemini-2.5-pro",),
    ).review(_sample_pr(), FileDump(entries=(), total_chars=0))

    assert [cmd[2] for cmd in calls] == [
        "gemini-3.1-pro-preview",
        "gemini-2.5-pro",
    ]
    assert result.summary == "recovered via fallback"
    # 스트림 절단으로 fallback 된 경우에도 실제 응답 모델이 주입돼야 한다.
    assert result.model == "gemini-2.5-pro"


def test_stream_close_markers_are_not_redundant() -> None:
    """`premature close` 와 `err_stream_premature_close` 는 서로 다른 출력 형태를 잡는다.

    회귀 방지: 두 마커가 중복처럼 보여 누군가 한 쪽을 지우면 실제 관측되는 Node.js
    출력 형태 중 하나가 마킹을 빠져나간다. 매칭은 부분 문자열 기반이므로 공백 vs
    언더스코어 차이로 서로를 포함하지 않는다는 사실을 테스트로 고정한다.
    """
    prose_form = "Error: Premature close"  # Node.js 가 사람이 읽는 메시지로 내는 형태
    code_form = "code: 'ERR_STREAM_PREMATURE_CLOSE'"  # code 필드의 상수 형태

    # 양쪽 다 retryable 로 인식돼야 한다 (fallback 경로 발동)
    assert _is_retryable_model_failure(prose_form) is True
    assert _is_retryable_model_failure(code_form) is True

    # 구조적 이유로 둘은 서로를 포함하지 않는다 (공백 vs 언더스코어):
    # "premature close" not in "err_stream_premature_close" 이고 반대도 성립하지 않는다.
    # 따라서 두 마커 중 하나만 남기면 나머지 형태는 커버리지에서 빠진다.
    assert "premature close" not in code_form.lower()
    assert "err_stream_premature_close" not in prose_form.lower()


def test_review_falls_back_on_subprocess_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`subprocess.TimeoutExpired` 가 fallback 체인을 우회하지 않고 다음 모델로 넘어가야 한다.

    회귀 방지: timeout 은 stderr 마커가 아니라 Python 예외로 도착해 `_is_retryable_model_failure`
    검사를 거치지 않는다. 만약 `_invoke_review` 내부에서 RuntimeError 로 변환하면 `review()`
    의 model 루프 자체를 빠져나가 fallback 이 발동 못하고 PR 이 조용히 유실된다 (실관측됨).
    """
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeCompleted:
        calls.append(cmd)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))
        return _FakeCompleted(
            0,
            '{"summary": "recovered after timeout", "event": "COMMENT", "comments": []}',
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GeminiCliEngine(
        binary="gemini",
        model="gemini-3.1-pro-preview",
        fallback_models=("gemini-2.5-pro",),
        timeout_sec=600,
    ).review(_sample_pr(), FileDump(entries=(), total_chars=0))

    assert [cmd[2] for cmd in calls] == [
        "gemini-3.1-pro-preview",
        "gemini-2.5-pro",
    ]
    assert result.summary == "recovered after timeout"
    assert result.model == "gemini-2.5-pro"


def test_review_raises_when_all_models_time_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fallback 체인의 모든 모델이 timeout 인 경우엔 RuntimeError 로 끝나야 한다.

    안전망: 첫 모델만 timeout → fallback 성공이 정상 경로지만, **마지막 모델까지** timeout
    이면 더 이상 시도할 곳이 없으므로 명시적 RuntimeError 로 _process 의 ERROR 로깅 경로에
    들어가야 한다 (운영자가 timeout 한도/네트워크 환경을 점검할 신호).
    """
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeCompleted:
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match=r"timed out .* on all 2 model"):
        GeminiCliEngine(
            binary="gemini",
            model="gemini-3.1-pro-preview",
            fallback_models=("gemini-2.5-pro",),
            timeout_sec=600,
        ).review(_sample_pr(), FileDump(entries=(), total_chars=0))

    # 두 모델 모두 호출돼야 하며 (체인 끝까지 시도), 그 이후에도 더 시도하지 않는다.
    assert [cmd[2] for cmd in calls] == [
        "gemini-3.1-pro-preview",
        "gemini-2.5-pro",
    ]


def test_review_drops_findings_on_paths_outside_pr_changed_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """엔진이 `pr.changed_files` 를 valid_paths 로 전달해 환각 path finding 을 드롭한다.

    회귀 방지: parse_review 에 valid_paths 가 안 넘어가면 모델이 만든 가짜 path finding
    이 그대로 살아남아 본문 surface 또는 잘못된 인라인 시도로 이어진다. 이 테스트는
    엔진→파서 배선이 항상 changed_files 를 함께 넘기는지 고정한다.
    """

    def fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        return _FakeCompleted(
            0,
            (
                '{"summary": "ok", "event": "COMMENT", "comments": ['
                '{"path": "src/a.py", "line": 1, "body": "[Minor] 실재"},'
                '{"path": "tests/imaginary.py", "line": 1, "body": "[Critical] 가짜"}'
                "]}"
            ),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    pr = PullRequest(
        repo=RepoRef("o", "r"),
        number=1,
        title="t",
        body="",
        head_sha="abc",
        head_ref="feat",
        base_sha="def",
        base_ref="main",
        clone_url="https://example.com/o/r.git",
        changed_files=("src/a.py",),  # `tests/imaginary.py` 는 PR 에 없음
        installation_id=7,
        is_draft=False,
    )

    result = GeminiCliEngine(binary="gemini", model="gemini-2.5-pro").review(
        pr, FileDump(entries=(), total_chars=0)
    )

    paths = [f.path for f in result.findings]
    assert paths == ["src/a.py"], (
        "PR changed_files 밖의 finding 은 엔진이 파서에 전달한 valid_paths 로 드롭돼야 함"
    )
