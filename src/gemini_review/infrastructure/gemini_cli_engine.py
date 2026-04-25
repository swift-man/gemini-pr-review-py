import dataclasses
import json
import logging
import subprocess
from pathlib import Path

from gemini_review.domain import FileDump, PullRequest, ReviewResult

from .gemini_parser import parse_review
from .gemini_prompt import build_diff_prompt, build_prompt, paths_in_pr_diff

# diff fallback 시 ReviewResult.summary 에 prepend 되는 사용자 안내 (gemini PR #26
# review #4 권고). PR 본문 상단에 이 문구가 노출돼 리뷰 수신자가 "전체 코드가 아니라
# 변경 라인만 본 narrower 리뷰" 임을 인지하고 cross-file 단언은 보수적으로 참고.
_DIFF_MODE_SUMMARY_NOTICE = (
    "⚠️ **이 리뷰는 diff-only fallback 모드로 작성되었습니다** "
    "(전체 코드베이스가 컨텍스트 한도를 초과). cross-file 영향·외부 모듈 사용처는 "
    "검증되지 않았으니 보수적으로 참고하세요.\n\n"
)

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
        # 전체 모드의 valid_paths 는 PR 전체 변경 파일 — 일반 흐름은 모든 변경 파일에
        # 대해 finding 을 받는 게 정상.
        return self._run_with_model_fallback(
            pr=pr,
            prompt=prompt,
            valid_paths=frozenset(pr.changed_files),
            invoke_log_kv={
                "files": len(dump.entries),
                "chars": dump.total_chars,
                "mode": "full",
            },
        )

    def review_diff(self, pr: PullRequest, diff_text: str) -> ReviewResult:
        """전체 코드베이스 컨텍스트 한도 초과 시의 fallback — diff 만으로 리뷰 수행.

        프롬프트가 모델에게 cross-file 단언 금지 + [Critical]/[Major] 등급 절제를
        강하게 안내하므로, 같은 모델/타임아웃/fallback chain 정책을 그대로 재사용한다.
        결과 ReviewResult 의 형식은 `review()` 와 동일 — 호출부 (use case) 가 같은
        후처리 (verify, dedupe, post) 흐름을 적용 가능.

        ### 차이 (codex/gemini PR #26 review #4)

        1. valid_paths 가 `paths_in_pr_diff(pr)` — `assemble_pr_diff` 에 실제 포함된
           파일만 인정. 삭제-only / binary / truncate 처럼 diff 입력에서 제외된 파일
           에 대한 환각 finding 이 PR METADATA 만 보고 만들어져도 파서에서 드롭됨.
        2. ReviewResult.summary 에 diff-only 모드 안내 prepend — 리뷰 수신자가 본문
           상단에서 narrower 리뷰임을 즉시 인지.
        """
        prompt = build_diff_prompt(pr, diff_text)
        valid_paths = paths_in_pr_diff(pr)
        result = self._run_with_model_fallback(
            pr=pr,
            prompt=prompt,
            valid_paths=valid_paths,
            invoke_log_kv={
                "diff_chars": len(diff_text),
                "files": len(valid_paths),
                "mode": "diff",
            },
        )
        # 사용자 가시성: 본문 상단에 모드 명시 — 후처리 (verify/dedupe) 는 summary 를
        # 건드리지 않으므로 여기서 prepend 하면 최종 게시까지 그대로 노출됨.
        return dataclasses.replace(
            result, summary=_DIFF_MODE_SUMMARY_NOTICE + result.summary
        )

    def _run_with_model_fallback(
        self,
        *,
        pr: PullRequest,
        prompt: str,
        valid_paths: frozenset[str],
        invoke_log_kv: dict[str, object],
    ) -> ReviewResult:
        """모델 fallback chain 으로 prompt 를 태우는 공유 루프.

        review() / review_diff() 두 진입점이 같은 retryable 실패 정책 (timeout, empty
        stdout, capacity/preview 실패 마커) 으로 fallback 모델을 순회하도록 한 곳에
        모았다. invoke_log_kv 는 진단 로그용 모드별 메타.

        valid_paths 는 파서의 path grounding 입력 — 모델이 실제 컨텍스트에 없던 파일
        이름을 만들어 내면 finding 단계에서 드롭. review() 는 PR 전체 changed_files,
        review_diff() 는 diff 에 실제 포함된 파일 (codex PR #26 review #4) 로 좁혀짐.
        """
        models = _dedupe_models(self._model, self._fallback_models)
        last_error = ""
        for index, model in enumerate(models):
            has_fallback = index + 1 < len(models)

            # TimeoutExpired 는 Python 예외라 stderr 마커 검사를 거치지 않는다. preview 모델이
            # 긴 프롬프트에 응답 못 하는 건 전형적 retryable 실패이므로 여기서 잡아 fallback
            # 체인에 태운다. 이 블록을 _invoke_review 내부에서 RuntimeError 로 변환하면
            # 루프 자체를 빠져나가 fallback 이 발동하지 않는 회귀가 생긴다.
            try:
                result = self._invoke_review(model, prompt, invoke_log_kv)
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
                # 빈 stdout 은 "성공인데 무응답" 이라는 모순 상태. 실관측: gemini-3.1-pro-preview
                # 가 일부 PR (예: mlx-pr-review-py#31) 에 returncode=0 + 완전 빈 stdout 으로
                # 응답. 그대로 파서에 넘기면 빈 ReviewResult 가 만들어져 "Gemini 응답을
                # 파싱하지 못했습니다." 라는 무의미한 리뷰가 GitHub 에 게시됨. fallback 체인을
                # 태워 다음 모델이 실제 응답을 줄 기회를 만든다.
                if not result.stdout.strip():
                    last_error = "empty stdout (model returned no content)"
                    if has_fallback:
                        fallback = models[index + 1]
                        logger.warning(
                            "gemini %s returned empty stdout (returncode=0); "
                            "falling back to %s",
                            model,
                            fallback,
                        )
                        continue
                    # 진단 보강 (codex PR #24 review): 마지막 모델명과 stderr preview 를
                    # 메시지에 포함해 운영자가 어떤 모델이 마지막에 실패했고 그 시점의
                    # 부가 정보 (예: 토큰 한도 도달 메시지) 가 무엇이었는지 즉시 확인 가능.
                    # stderr 는 보통 진단 정보 (모델 상태/에러) 를 담아 시크릿 위험은 낮지만
                    # preview 길이는 200 자로 제한해 만일의 누출 표면 줄임.
                    stderr_preview = (result.stderr or "").strip()[:200]
                    raise RuntimeError(
                        f"gemini -p returned empty stdout on all {len(models)} model(s); "
                        f"last_model={model}, stderr_preview={stderr_preview!r}"
                    )
                # `parse_review` 는 CLI 출력만 해석하므로 어느 모델이 이 결과를 만들었는지
                # 모른다. 여기서 한 번에 주입해서 fallback 발동 시 운영자가 본문 푸터로
                # 실제 사용 모델을 바로 확인할 수 있게 한다.
                # `valid_paths` 는 path grounding — 모델이 컨텍스트에 없던 파일 이름을
                # 만들어 내면 finding 단계에서 드롭. caller (`review`/`review_diff`) 가
                # 모드에 맞는 set 을 주입 (codex PR #26 review #4: diff 모드에선
                # changed_files 전체가 아니라 diff 입력 실제 포함된 파일로 좁혀짐).
                parsed = parse_review(result.stdout, valid_paths=valid_paths)
                return dataclasses.replace(parsed, model=model)

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
        log_kv: dict[str, object],
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
            "invoking gemini: %s model=%s",
            " ".join(f"{k}={v}" for k, v in log_kv.items()),
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
