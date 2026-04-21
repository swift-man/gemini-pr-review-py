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

# 종료 시 워커가 현재 작업을 끝낼 때까지 기다리는 기본 시간.
# 대형 리뷰(gemini CLI 5~10분)는 이 안에 안 끝나지만, idle 큐 drain 과 짧은 리뷰
# (캐시 히트 / 초기 에러 등) 는 충분히 소화한다. uvicorn 의 shutdown 예산 안에
# 들어오도록 너무 길게 잡지 않는다.
_DEFAULT_STOP_TIMEOUT_SEC = 10.0


@dataclass(frozen=True)
class WebhookJob:
    delivery_id: str
    repo: RepoRef
    number: int
    installation_id: int

    def __str__(self) -> str:
        # stop() 의 드롭 로그와 운영 관측성에 쓰이는 공용 포맷. delivery_id 와 PR 식별자가
        # 함께 찍혀야 GitHub Recent Deliveries 와 교차 대조 + 재시도 대상 식별이 가능.
        return f"{self.delivery_id}({self.repo.full_name}#{self.number})"


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
        # 현재 처리 중인 작업 — 종료 시 "어떤 리뷰가 드롭됐는지" 가시화용.
        # 데이터 레이스를 피하려면 이 필드는 _in_flight_lock 안에서만 읽고 쓴다.
        self._in_flight_lock = threading.Lock()
        self._in_flight: WebhookJob | None = None

    # --- 라이프사이클 --------------------------------------------------------

    def start(self) -> None:
        if self._worker is not None:
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._run, name="review-worker", daemon=True
        )
        self._worker.start()

    def stop(self, timeout: float = _DEFAULT_STOP_TIMEOUT_SEC) -> None:
        """워커에게 중단 신호를 보낸 뒤 `timeout` 초까지 완료를 기다린다.

        단순히 플래그만 세우던 기존 동작은 `daemon=True` 스레드를 즉시 종료시켜
        리뷰 중이던 작업이 GitHub 에 202 로 접수된 채 영구 유실됐다.
        (우선순위 #3 — 데이터 손실 / 상태 불일치)

        본 구현은 데몬 스레드를 유지해 uvicorn 이 끝내 종료될 수 있게 하면서,
        idle 큐 drain 과 짧은 리뷰가 자연스럽게 끝날 시간을 준다. gemini CLI 호출
        중인 대형 리뷰는 timeout 을 초과해도 드롭되지만, 그 때는 **어떤 작업이
        유실됐는지** 를 ERROR 로그에 명시해 운영자가 재시도(빈 커밋 push 등) 할
        근거를 남긴다.
        """
        worker = self._worker
        if worker is None:
            return

        with self._in_flight_lock:
            in_flight_snapshot = self._in_flight
        queued_before = self._queue.qsize()

        if in_flight_snapshot is not None or queued_before > 0:
            logger.warning(
                "shutting down with pending work — in_flight=%s, queued=%d; "
                "waiting up to %.1fs for graceful completion",
                in_flight_snapshot or "none",
                queued_before,
                timeout,
            )

        self._stop.set()
        worker.join(timeout=timeout)

        if worker.is_alive():
            # 워커가 timeout 안에 안 끝남 — 데몬이라 프로세스 종료 시 강제로 죽는다.
            # worker 는 이 시점 이전에 이미 _stop 신호로 get() 루프를 벗어났어야 하지만,
            # _process 내부 블로킹(gemini CLI) 으로 묶여 있는 경우도 있다. 어쨌든 큐에는
            # 더 이상 손대지 않으므로 내부 deque 스냅샷이 안정.
            with self._in_flight_lock:
                still_in_flight = self._in_flight
            remaining_jobs = self._snapshot_queue()
            logger.error(
                "worker did not finish within %.1fs; review in-flight=%s, "
                "remaining_queue=[%s] may be lost. "
                "operator: re-trigger the affected PR(s) with an empty commit.",
                timeout,
                still_in_flight or "none",
                ", ".join(str(job) for job in remaining_jobs) or "empty",
            )
            # _worker 는 일부러 정리하지 않는다. 스레드 객체가 아직 살아 있는 상태에서
            # 레퍼런스를 지우면 후속 start() 가 좀비 옆에 새 워커를 띄워 중복 실행된다.
            return

        # 정상 종료 — 인스턴스가 start() 로 재사용될 수 있도록 스레드 레퍼런스 초기화.
        # (운영상 같은 인스턴스 재사용은 드물지만, lifespan 이 restart 되거나 테스트가
        #  한 인스턴스를 stop/start 순서로 검증하는 경우 필요.)
        self._worker = None

    def _snapshot_queue(self) -> list[WebhookJob]:
        """락을 잡고 내부 deque 를 얕은 복사로 찍는다 — 드롭 대상 식별용.

        `Queue.mutex` 는 문서화된 락이며 여기서 잠깐 잡아 스냅샷만 떠 나와도 워커가
        어차피 `_stop` 이후 get() 하지 않으므로 경합 위험이 없다. `qsize()` 만으로는
        "몇 개가 있었는지" 밖에 모르고 "어떤 PR 이었는지" 를 놓치는데, 드롭 대상 재시도
        안내가 이 PR 의 핵심 목적이라 식별자가 필요.
        """
        with self._queue.mutex:
            return list(self._queue.queue)

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
        with self._in_flight_lock:
            self._in_flight = job
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
        finally:
            # in-flight 기록은 성공·실패·예외 모두에서 정확히 지워져야 stop() 시점의
            # "드롭된 작업" 로그가 거짓 양성(false positive) 으로 안 찍힌다.
            with self._in_flight_lock:
                self._in_flight = None
