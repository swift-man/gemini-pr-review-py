import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from gemini_review.infrastructure.gemini_cli_engine import GeminiAuthError, GeminiCliEngine


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
