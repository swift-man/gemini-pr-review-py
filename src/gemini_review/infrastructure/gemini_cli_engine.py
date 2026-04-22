import dataclasses
import json
import logging
import subprocess
from pathlib import Path

from gemini_review.domain import FileDump, PullRequest, ReviewResult

from .gemini_parser import parse_review
from .gemini_prompt import build_prompt

logger = logging.getLogger(__name__)

_STDIN_PROMPT_PLACEHOLDER = " "
_DEFAULT_FALLBACK_MODELS = ("gemini-2.5-pro",)
_RETRYABLE_MODEL_FAILURE_MARKERS = (
    # 용량 / 레이트 관련 — preview 모델이 포화됐거나 무료 티어 한도에 닿은 경우
    "429",
    "model_capacity_exhausted",
    "no capacity available",
    "rate limit exceeded",
    "ratelimitexceeded",
    "resource_exhausted",
    "too many requests",
    # 스트림 / 네트워크 절단 — preview 모델이 긴 응답 중에 Google 서버에서 끊어버리는 경우가
    # 실관측됨(`ERR_STREAM_PREMATURE_CLOSE`). 모델 쪽 일시 불안정에 가까우므로 같은 모델 재시도가
    # 아닌 안정 fallback 모델로 바로 넘기는 게 타당하다.
    "premature close",
    "err_stream_premature_close",
    "econnreset",
    "socket hang up",
)


class GeminiAuthError(RuntimeError):
    """Gemini CLI 가 Google OAuth 로 인증되지 않았을 때 발생하는 예외."""


class GeminiCliEngine:
    """stdin 으로 Gemini CLI (`gemini -p`) 를 호출해 JSON 리뷰를 파싱해 돌려준다.

    인증은 Google OAuth 기반 — 운영자가 터미널에서 `gemini` 를 한 번 실행해 브라우저 로그인을
    마치면 `~/.gemini/oauth_creds.json` 에 리프레시 토큰이 저장되고, 이후 호출은 이 파일로 세션을
    재개한다. 서버는 브라우저 플로우를 돌릴 수 없으므로 "파일이 있어야만 기동"을 보장한다.
    """

    def __init__(
        self,
        binary: str = "gemini",
        model: str = "gemini-2.5-pro",
        fallback_models: tuple[str, ...] = _DEFAULT_FALLBACK_MODELS,
        timeout_sec: int = 600,
        oauth_creds_path: Path | None = None,
    ) -> None:
        self._binary = binary
        self._model = model
        self._fallback_models = fallback_models
        self._timeout_sec = timeout_sec
        # 기본값은 Gemini CLI 가 생성하는 표준 위치. 테스트/커스텀 설치 경로는 DI 로 교체 가능.
        self._oauth_creds_path = oauth_creds_path or Path.home() / ".gemini" / "oauth_creds.json"

    def verify_auth(self) -> str:
        """기동 전 두 단계 사전 점검 — 바이너리가 실행 가능하고 Google OAuth creds 가 존재.

        Gemini CLI 는 `auth status` 같은 전용 명령이 없어 파일 존재 + `--version` 응답으로
        확정한다. 파일만 있고 만료됐을 가능성은 실제 호출에서 드러나도록 둔다(리뷰 호출 시
        오류가 뜨면 운영자가 재로그인). 기동 시점엔 "현재까지 알려진 정상 상태" 확인이면 충분.
        """
        version = self._probe_binary()
        self._probe_oauth_creds()
        return f"gemini {version} (oauth creds: {self._oauth_creds_path})"

    def _probe_binary(self) -> str:
        try:
            result = subprocess.run(  # noqa: S603
                [self._binary, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except FileNotFoundError as exc:
            raise GeminiAuthError(
                f"GEMINI_BIN='{self._binary}' 을(를) 실행할 수 없습니다. "
                "경로를 확인하거나 `npm i -g @google/gemini-cli` 로 설치하세요."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GeminiAuthError("gemini --version 이 10초 내에 응답하지 않았습니다.") from exc

        if result.returncode != 0:
            raise GeminiAuthError(
                "Gemini CLI 실행에 실패했습니다.\n"
                f"출력: {(result.stdout + result.stderr).strip()[:500] or '(empty)'}"
            )
        return (result.stdout or result.stderr).strip().splitlines()[0] if (
            result.stdout or result.stderr
        ) else "unknown"

    def _probe_oauth_creds(self) -> None:
        creds = self._oauth_creds_path
        if not creds.exists():
            raise GeminiAuthError(
                "Google OAuth 자격 증명 파일이 없습니다.\n"
                f"예상 경로: {creds}\n"
                f"해결: 터미널에서 `{self._binary}` 를 한 번 실행해 Google 계정으로 로그인하세요. "
                "(브라우저가 열리고 동의가 끝나면 creds 파일이 생성됩니다.)"
            )
        try:
            with creds.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, json.JSONDecodeError) as exc:
            raise GeminiAuthError(
                f"OAuth 자격 증명 파일({creds})을 읽지 못했습니다: {exc}\n"
                f"해결: 파일을 삭제하고 `{self._binary}` 를 다시 실행해 로그인하세요."
            ) from exc

        # refresh_token 은 Google OAuth 장기 자격의 핵심 — 없으면 세션 재개 불가.
        if not isinstance(data, dict) or not data.get("refresh_token"):
            raise GeminiAuthError(
                f"OAuth 자격 증명에 refresh_token 이 없습니다({creds}).\n"
                f"해결: 파일을 삭제하고 `{self._binary}` 를 다시 실행해 로그인하세요."
            )

    def review(self, pr: PullRequest, dump: FileDump) -> ReviewResult:
        prompt = build_prompt(pr, dump)
        models = _dedupe_models(self._model, self._fallback_models)
        last_error = ""
        for index, model in enumerate(models):
            has_fallback = index + 1 < len(models)

            # TimeoutExpired 는 Python 예외라 stderr 마커 검사를 거치지 않는다. preview 모델이
            # 긴 프롬프트에 응답 못 하는 건 전형적 retryable 실패이므로 여기서 잡아 fallback
            # 체인에 태운다. 이 블록을 _invoke_review 내부에서 RuntimeError 로 변환하면
            # 루프 자체를 빠져나가 fallback 이 발동하지 않는 회귀가 생긴다.
            try:
                result = self._invoke_review(model, prompt, dump)
            except subprocess.TimeoutExpired:
                if not has_fallback:
                    raise RuntimeError(
                        f"gemini -p timed out after {self._timeout_sec}s on all "
                        f"{len(models)} model(s)"
                    ) from None
                fallback = models[index + 1]
                logger.warning(
                    "gemini timed out after %ds on %s; falling back to %s",
                    self._timeout_sec,
                    model,
                    fallback,
                )
                last_error = f"timed out after {self._timeout_sec}s"
                continue

            if result.returncode == 0:
                # `parse_review` 는 CLI 출력만 해석하므로 어느 모델이 이 결과를 만들었는지
                # 모른다. 여기서 한 번에 주입해서 fallback 발동 시 운영자가 본문 푸터로
                # 실제 사용 모델을 바로 확인할 수 있게 한다.
                return dataclasses.replace(parse_review(result.stdout), model=model)

            last_error = _combined_output(result)
            if has_fallback and _is_retryable_model_failure(last_error):
                fallback = models[index + 1]
                logger.warning(
                    "gemini model failed; falling back from %s to %s: %s",
                    model,
                    fallback,
                    last_error[:500],
                )
                continue

            raise RuntimeError(_failure_message(model, result.returncode, last_error))

        # `_dedupe_models` 는 최소 primary 모델 하나를 보장한다. 이 분기는 타입 체커와 미래의
        # 방어적 변경을 위한 안전망이다.
        raise RuntimeError(f"gemini -p failed: {last_error[:1000] or '(empty)'}")

    def _invoke_review(
        self,
        model: str,
        prompt: str,
        dump: FileDump,
    ) -> subprocess.CompletedProcess[str]:
        # Gemini CLI 0.38.x 는 `-p/--prompt` 뒤에 문자열 인자를 요구한다. 실제 전체 레포
        # 프롬프트는 계속 stdin 으로 흘려 보내 ARG_MAX 를 피하고, argv 에는 non-empty
        # placeholder 만 둔다.
        # `-m` 은 모델 오버라이드. Gemini CLI 는 별도 reasoning-effort 플래그가 없어 생략.
        cmd = [
            self._binary,
            "-m",
            model,
            "-p",
            _STDIN_PROMPT_PLACEHOLDER,
        ]
        logger.info(
            "invoking gemini: files=%d chars=%d model=%s",
            len(dump.entries),
            dump.total_chars,
            model,
        )
        # TimeoutExpired 는 일부러 잡지 않고 그대로 propagate — caller 인 `review()` 가
        # fallback 체인 결정을 한 곳에 모아서 처리한다 (returncode≠0 케이스와 동일 정책).
        return subprocess.run(  # noqa: S603
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=self._timeout_sec,
            check=False,
        )


def _dedupe_models(primary: str, fallback_models: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    models: list[str] = []
    for model in (primary, *fallback_models):
        clean = model.strip()
        if clean and clean not in seen:
            seen.add(clean)
            models.append(clean)
    return tuple(models) or (primary,)


def _combined_output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr + "\n" + result.stdout).strip()


def _is_retryable_model_failure(output: str) -> bool:
    lower = output.lower()
    if any(marker in lower for marker in _RETRYABLE_MODEL_FAILURE_MARKERS):
        return True
    if "preview" not in lower:
        return False
    return any(marker in lower for marker in ("not found", "not supported", "unavailable"))


def _failure_message(model: str, returncode: int, output: str) -> str:
    return f"gemini -p failed with model {model} ({returncode}): {output[:1000]}"
