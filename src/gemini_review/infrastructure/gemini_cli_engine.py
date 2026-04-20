import json
import logging
import subprocess
from pathlib import Path

from gemini_review.domain import FileDump, PullRequest, ReviewResult

from .gemini_parser import parse_review
from .gemini_prompt import build_prompt

logger = logging.getLogger(__name__)


class GeminiAuthError(RuntimeError):
    """Raised when the Gemini CLI is not authenticated with Google OAuth."""


class GeminiCliEngine:
    """Calls the Gemini CLI (`gemini -p`) over stdin and parses a JSON review.

    인증은 Google OAuth 기반 — 운영자가 터미널에서 `gemini` 를 한 번 실행해 브라우저 로그인을
    마치면 `~/.gemini/oauth_creds.json` 에 리프레시 토큰이 저장되고, 이후 호출은 이 파일로 세션을
    재개한다. 서버는 브라우저 플로우를 돌릴 수 없으므로 "파일이 있어야만 기동"을 보장한다.
    """

    def __init__(
        self,
        binary: str = "gemini",
        model: str = "gemini-2.5-pro",
        timeout_sec: int = 600,
        oauth_creds_path: Path | None = None,
    ) -> None:
        self._binary = binary
        self._model = model
        self._timeout_sec = timeout_sec
        # 기본값은 Gemini CLI 가 생성하는 표준 위치. 테스트/커스텀 설치 경로는 DI 로 교체 가능.
        self._oauth_creds_path = oauth_creds_path or Path.home() / ".gemini" / "oauth_creds.json"

    def verify_auth(self) -> str:
        """Two-step preflight: binary is executable AND Google OAuth creds exist.

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
        # `-p` (prompt) 모드에서 stdin 으로 프롬프트를 흘려 보낸다.
        # argv 로 넘기지 않는 이유는 전체 레포 덤프가 수백 KB ~ 수 MB 라 ARG_MAX 를 초과할 수 있어서.
        # `-m` 은 모델 오버라이드. Gemini CLI 는 별도 reasoning-effort 플래그가 없어 생략.
        cmd = [
            self._binary,
            "-m",
            self._model,
            "-p",
        ]
        logger.info(
            "invoking gemini: files=%d chars=%d model=%s",
            len(dump.entries),
            dump.total_chars,
            self._model,
        )
        try:
            result = subprocess.run(  # noqa: S603
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self._timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"gemini -p timed out after {self._timeout_sec}s"
            ) from exc

        if result.returncode != 0:
            raise RuntimeError(
                f"gemini -p failed ({result.returncode}): {result.stderr.strip()[:1000]}"
            )

        return parse_review(result.stdout)
