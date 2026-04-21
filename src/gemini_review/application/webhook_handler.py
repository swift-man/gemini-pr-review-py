import hashlib
import hmac
import logging
import queue
import threading
from dataclasses import dataclass

from gemini_review.domain import RepoRef
from gemini_review.interfaces import GitHubClient
from gemini_review.logging_utils import get_delivery_logger

from .review_pr_use_case import ReviewPullRequestUseCase

logger = logging.getLogger(__name__)

_SUPPORTED_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


@dataclass(frozen=True)
class WebhookJob:
    delivery_id: str
    repo: RepoRef
    number: int
    installation_id: int


class WebhookHandler:
    """webhook 을 검증하고 리뷰 작업을 큐에 넣은 뒤 직렬로 소비한다."""

    def __init__(
        self,
        secret: str,
        github: GitHubClient,
        use_case: ReviewPullRequestUseCase,
    ) -> None:
        self._secret = secret.encode("utf-8")
        self._github = github
        self._use_case = use_case
        self._queue: queue.Queue[WebhookJob] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()

    # --- 라이프사이클 --------------------------------------------------------

    def start(self) -> None:
        if self._worker is not None:
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._run, name="review-worker", daemon=True
        )
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()

    # --- 서명 검증 ----------------------------------------------------------

    def verify_signature(self, signature_header: str | None, body: bytes) -> bool:
        # 원문 body 로 HMAC 을 계산해야 함. json.loads 후 재직렬화하면 키 순서/공백 차이로
        # 서명이 달라져 정상 요청을 거부하게 된다. 따라서 검증은 반드시 raw bytes 단계에서.
        if not signature_header or not signature_header.startswith("sha256="):
            return False
        expected = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        # hmac.compare_digest 는 타이밍 공격 방지용 상수-시간 비교.
        return hmac.compare_digest(signature_header.removeprefix("sha256="), expected)

    # --- 디스패치 -----------------------------------------------------------

    def accept(
        self,
        event: str,
        delivery_id: str,
        payload: object,
    ) -> tuple[int, str]:
        # payload 는 외부 JSON 경계에서 들어오므로 dict 가 아닐 수 있다(유효 HMAC + `[]` 등).
        # 호출부가 타입을 좁혀 넘겨주는 것을 신뢰하지 않고 이 메서드 안에서 한 번 더 검증한다.
        dlog = get_delivery_logger(__name__, delivery_id)
        if event == "ping":
            return 200, "pong"
        if event != "pull_request":
            dlog.info("ignoring event: %s", event)
            return 202, "ignored"
        if not isinstance(payload, dict):
            dlog.warning("payload is not a JSON object: %s", type(payload).__name__)
            return 400, "invalid-payload-shape"

        action = str(payload.get("action", ""))
        if action not in _SUPPORTED_ACTIONS:
            dlog.info("ignoring action: %s", action)
            return 202, "ignored-action"

        pr = payload.get("pull_request") or {}
        # webhook payload 의 draft 값과 실제 처리 시점의 PR 상태가 다를 수 있어
        # _process() 에서 한 번 더 확인한다(여기선 큐 진입 전 1차 필터).
        if bool(pr.get("draft")):
            dlog.info("skipping draft PR")
            return 202, "skipped-draft"

        repo_full = str(payload.get("repository", {}).get("full_name", ""))
        if "/" not in repo_full:
            dlog.warning("missing repository full_name in payload")
            return 400, "invalid-payload"
        owner, name = repo_full.split("/", 1)

        number = int(pr.get("number", 0))
        installation_id = int(payload.get("installation", {}).get("id", 0))
        if number == 0 or installation_id == 0:
            dlog.warning("missing number=%s or installation_id=%s", number, installation_id)
            return 400, "invalid-payload"

        job = WebhookJob(
            delivery_id=delivery_id,
            repo=RepoRef(owner=owner, name=name),
            number=number,
            installation_id=installation_id,
        )
        self._queue.put(job)
        dlog.info(
            "queued review for %s#%d (queue_depth=%d)",
            job.repo.full_name,
            job.number,
            self._queue.qsize(),
        )
        return 202, "queued"

    # --- 워커 ---------------------------------------------------------------

    def _run(self) -> None:
        # 완전 직렬화: 한 번에 한 리뷰만 돌린다(동시성 1). Gemini CLI 가 사용자 Google
        # OAuth 토큰을 공유하므로 동시 호출은 rate-limit 에 걸릴 위험이 크고, `gemini -p` 가
        # 리뷰 하나당 수 분 수준이라 큐가 길어져도 순차 처리가 운영상 단순하기 때문.
        while not self._stop.is_set():
            try:
                # timeout 으로 주기적으로 깨어나 _stop 을 확인. 이렇게 해야 서버 종료 시 블로킹 없이
                # 스레드가 빠져나온다.
                job = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            self._process(job)
            self._queue.task_done()

    def _process(self, job: WebhookJob) -> None:
        dlog = get_delivery_logger(__name__, job.delivery_id)
        try:
            dlog.info("processing %s#%d", job.repo.full_name, job.number)
            pr = self._github.fetch_pull_request(job.repo, job.number, job.installation_id)
            if pr.is_draft:
                dlog.info("skipping draft at fetch time")
                return
            self._use_case.execute(pr)
            dlog.info("done %s#%d", job.repo.full_name, job.number)
        except Exception:
            dlog.exception("review failed for %s#%d", job.repo.full_name, job.number)
