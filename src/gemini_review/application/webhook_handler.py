import hashlib
import hmac
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
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

# 기본 동시 리뷰 워커 수. `WebhookHandler(concurrency=...)` 로 주입하지 않은 경우에
# 한해서만 쓰이는 fallback — 운영은 `config.Settings.review_concurrency` 를 통한다.
_DEFAULT_CONCURRENCY = 3


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
    """webhook 을 검증하고 리뷰 작업을 ThreadPoolExecutor 에 올린다.

    병렬 정책:
    - 서로 다른 레포의 리뷰는 최대 `concurrency` 만큼 동시 실행.
    - **같은 레포** 에 쌓이는 리뷰는 `_repo_locks` 로 직렬화 — git 캐시 디렉터리
      `~/.gemini-review/repos/{owner}/{name}` 의 동시 `git clone`/`fetch`/`checkout`
      경합을 방지하기 위함. 이 락은 repo full_name 당 1개, 첫 접근 시 lazy 하게 생성.
    """

    def __init__(
        self,
        secret: str,
        github: GitHubClient,
        use_case: ReviewPullRequestUseCase,
        concurrency: int = _DEFAULT_CONCURRENCY,
    ) -> None:
        if concurrency < 1:
            raise ValueError(f"concurrency must be >= 1, got {concurrency}")
        self._secret = secret.encode("utf-8")
        self._github = github
        self._use_case = use_case
        self._concurrency = concurrency

        # executor 와 관련 상태는 start/stop 시 교체되므로 전용 락으로 보호한다.
        self._executor_lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None

        # 진행 중(running 중인 _process) 작업 추적. 종료 시 "어떤 리뷰가 드롭됐는지"
        # 를 ERROR 로그로 가시화하기 위한 관측성 구조.
        self._in_flight_lock = threading.Lock()
        self._in_flight: dict[str, WebhookJob] = {}

        # submit 된 future → 대응 WebhookJob 매핑. 종료 시 cancel 된 queued future 의
        # 식별자를 로그로 남기기 위해 유지. done 콜백으로 자동 정리.
        self._pending_lock = threading.Lock()
        self._pending: dict[Future[None], WebhookJob] = {}

        # 레포별 직렬화 락. 같은 레포에 대한 _process 호출은 반드시 이 락 안에서 실행.
        self._repo_locks_lock = threading.Lock()
        self._repo_locks: dict[str, threading.Lock] = {}

    # --- 라이프사이클 --------------------------------------------------------

    def start(self) -> None:
        with self._executor_lock:
            if self._executor is not None:
                return
            self._executor = ThreadPoolExecutor(
                max_workers=self._concurrency,
                thread_name_prefix="review-worker",
            )

    def stop(self, timeout: float = _DEFAULT_STOP_TIMEOUT_SEC) -> None:
        """워커에게 중단 신호를 보낸 뒤 `timeout` 초까지 완료를 기다린다.

        단순히 플래그만 세우던 기존 동작은 `daemon=True` 스레드를 즉시 종료시켜
        리뷰 중이던 작업이 GitHub 에 202 로 접수된 채 영구 유실됐다.
        (우선순위 #3 — 데이터 손실 / 상태 불일치)

        본 구현은:
        - 아직 시작되지 않은 queued future 를 즉시 취소 (`cancel_futures=True`)
        - 이미 실행 중인 future 는 `timeout` 초까지 완료를 대기
        - 마감 내 못 끝난 running / 취소된 queued 식별자를 **모두 ERROR 로그로**
          남겨 운영자가 영향받은 PR 에 빈 커밋 push 등으로 재시도 가능하도록.
        """
        with self._executor_lock:
            executor = self._executor
            if executor is None:
                return
            # 이후 `_submit` 이 새 작업을 밀어 넣지 않도록 레퍼런스를 먼저 떼어낸다.
            self._executor = None

        # 종료 시점 스냅샷 — 아래 분석의 기준이 된다.
        with self._pending_lock:
            pending_snapshot = dict(self._pending)

        running = [f for f, _ in pending_snapshot.items() if f.running()]
        not_started = [
            f for f, _ in pending_snapshot.items() if not f.running() and not f.done()
        ]

        if running or not_started:
            logger.warning(
                "shutting down with pending work — running=%d, queued=%d; "
                "waiting up to %.1fs for graceful completion",
                len(running),
                len(not_started),
                timeout,
            )

        # 아직 시작 안 된 future 는 즉시 cancel, 실행 중인 것은 자연 완료 대기.
        executor.shutdown(wait=False, cancel_futures=True)
        if running:
            wait(running, timeout=timeout)

        # 마감 후에도 안 끝난 실행 중 작업 + 취소된 queued 작업을 로그에 남긴다.
        still_running_jobs = [
            pending_snapshot[f] for f in running if not f.done()
        ]
        cancelled_jobs = [
            pending_snapshot[f] for f in not_started if f.cancelled() or not f.done()
        ]

        if still_running_jobs or cancelled_jobs:
            logger.error(
                "worker did not finish within %.1fs; "
                "running=[%s], cancelled_queued=[%s] may be lost. "
                "operator: re-trigger the affected PR(s) with an empty commit.",
                timeout,
                ", ".join(str(j) for j in still_running_jobs) or "none",
                ", ".join(str(j) for j in cancelled_jobs) or "none",
            )

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
        submitted = self._submit(job)
        if not submitted:
            # start() 전이거나 stop() 이후 — 이 경우 운영상 이슈이므로 GitHub 가
            # 재전송을 시도하도록 5xx 대신 명시적 4xx 로 돌려주지 않고, 그냥 202 ignored
            # 로 처리하면 GitHub 쪽에는 성공처럼 보여 더 조용히 유실된다. 굳이 구분하자.
            dlog.warning("handler not running — dropped job for %s#%d", repo_full, number)
            return 503, "not-running"

        dlog.info(
            "queued review for %s#%d (pending=%d)",
            job.repo.full_name,
            job.number,
            self._pending_count(),
        )
        return 202, "queued"

    # --- 내부 ---------------------------------------------------------------

    def _submit(self, job: WebhookJob) -> bool:
        """executor 에 job 을 submit 하고 pending 맵에 등록. executor 없으면 False."""
        with self._executor_lock:
            executor = self._executor
            if executor is None:
                return False
            future = executor.submit(self._process, job)
        with self._pending_lock:
            self._pending[future] = job
        future.add_done_callback(self._on_future_done)
        return True

    def _on_future_done(self, future: Future[None]) -> None:
        with self._pending_lock:
            self._pending.pop(future, None)

    def _pending_count(self) -> int:
        with self._pending_lock:
            return len(self._pending)

    def _get_repo_lock(self, repo: RepoRef) -> threading.Lock:
        """레포별 직렬화 락. 같은 full_name 으로 오는 리뷰는 이 락을 공유한다.

        락 맵은 소거하지 않는다 — threading.Lock 자체는 수십 바이트이고, 한 번 리뷰된
        레포가 사라지는 시나리오가 없어 누수 우려 없음. 필요해지면 LRU 로 감쌀 것.
        """
        with self._repo_locks_lock:
            return self._repo_locks.setdefault(repo.full_name, threading.Lock())

    def _process(self, job: WebhookJob) -> None:
        dlog = get_delivery_logger(__name__, job.delivery_id)
        with self._in_flight_lock:
            self._in_flight[job.delivery_id] = job
        try:
            # 같은 레포 안에서의 git 캐시 경합을 피하기 위한 직렬화. 서로 다른 레포의
            # 리뷰는 이 락을 공유하지 않으므로 병렬로 돌아간다.
            with self._get_repo_lock(job.repo):
                dlog.info("processing %s#%d", job.repo.full_name, job.number)
                pr = self._github.fetch_pull_request(
                    job.repo, job.number, job.installation_id
                )
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
                self._in_flight.pop(job.delivery_id, None)
