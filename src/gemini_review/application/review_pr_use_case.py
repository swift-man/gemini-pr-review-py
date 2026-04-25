import logging

from gemini_review.domain import FileDump, PullRequest, ReviewResult, TokenBudget
from gemini_review.infrastructure.gemini_prompt import assemble_pr_diff, build_diff_prompt
from gemini_review.interfaces import (
    FileCollector,
    FindingDeduper,
    FindingVerifier,
    GitHubClient,
    RepoFetcher,
    ReviewEngine,
)

logger = logging.getLogger(__name__)


class ReviewPullRequestUseCase:
    """리뷰 파이프라인 오케스트레이션.

    PR 조회 → 체크아웃 → 파일 수집 → 리뷰 → 출처 검증 → 이전 push dedup → 게시.
    """

    def __init__(
        self,
        github: GitHubClient,
        repo_fetcher: RepoFetcher,
        file_collector: FileCollector,
        engine: ReviewEngine,
        finding_verifier: FindingVerifier,
        finding_deduper: FindingDeduper,
        max_input_tokens: int,
    ) -> None:
        self._github = github
        self._repo_fetcher = repo_fetcher
        self._file_collector = file_collector
        self._engine = engine
        self._finding_verifier = finding_verifier
        self._finding_deduper = finding_deduper
        self._budget = TokenBudget(max_tokens=max_input_tokens)

    def execute(self, pr: PullRequest) -> None:
        token = self._github.get_installation_token(pr.installation_id)
        repo_path = self._repo_fetcher.checkout(pr, token)

        dump = self._file_collector.collect(repo_path, pr.changed_files, self._budget)

        # 변경 파일이 예산 때문에 잘려 나갔다면 "전체 리뷰"는 성립하지 않는다. 그래도 리뷰를
        # 완전히 건너뛰는 대신 **diff-only fallback** 으로 우회 시도. PR 전체 코드베이스가 모델
        # 컨텍스트를 초과하는 큰 저장소도 변경 라인 자체는 거의 항상 모델 한도 안에 들어오므로,
        # "리뷰 0건" 보다 "diff 만 본 narrower 리뷰" 가 사용자 가치 큼.
        #
        # 우선순위:
        #   1) 일반 모드 (변경 파일 모두 dump 에 들어감) — `engine.review(pr, dump)`
        #   2) diff fallback (변경 파일이 잘려 나감 + diff 가 비어 있지 않고 budget 안) —
        #      `engine.review_diff(pr, diff_text)`
        #   3) notice (diff 도 비었거나 너무 큼) — `_budget_exceeded_message`
        if dump.exceeded_budget and _changed_missing(pr, dump):
            result = self._fallback_to_diff_review(pr, dump)
            if result is None:
                return
        else:
            logger.info(
                "reviewing %s#%d — files=%d chars=%d excluded=%d",
                pr.repo.full_name,
                pr.number,
                len(dump.entries),
                dump.total_chars,
                len(dump.excluded),
            )
            result = self._engine.review(pr, dump)
        # Layer B — 출처 grounding: 모델이 본문에 인용한 텍스트가 실제 소스 라인에 존재
        # 하는지 디스크 레벨로 확인. phantom quote 환각 (예: 모델이 `"@scope"` 를
        # `" @scope"` 로 잘못 토큰화 → "원본에 공백" 단언) 을 [Suggestion] 으로 강등.
        result = self._finding_verifier.verify(result, repo_path)
        # Layer D — history grounding: 같은 PR 의 이전 push 에서 본 봇이 이미 게시했던
        # 동일 [Critical]/[Major] finding 은 메인테이너가 무시한 신호로 보고 [Suggestion]
        # 으로 강등. 4 회 연속 push 동일 phantom 코멘트 같은 alert fatigue 방어.
        result = self._finding_deduper.dedupe(result, pr)
        self._github.post_review(pr, result)


    def _fallback_to_diff_review(
        self,
        pr: PullRequest,
        dump: FileDump,
    ) -> ReviewResult | None:
        """전체 dump 가 예산 초과로 변경 파일을 다 못 담을 때의 우회 경로.

        Returns:
            성공 시 ReviewResult (caller 가 verify/dedupe/post 흐름으로 진행).
            None 이면 fallback 도 불가 → caller 는 일찍 return (notice 는 본 함수가
            이미 게시).

        ### 의도된 graceful degrade

        - diff 가 비었거나 (`assemble_pr_diff` 가 모두 binary/truncate 라 None 반환):
          애초에 모델이 볼 게 없음 → 기존 notice.
        - diff 는 있지만 **build_diff_prompt 로 합친 실제 입력 크기** (SYSTEM_RULES +
          DIFF_MODE_NOTICE + PR 메타 + diff 본문) 가 char 예산 초과: 모델 호출이
          어차피 실패할 가능성 큼 → notice (engine 호출 비용/noise 절감). diff_text
          본문 길이만 검사하면 fixed prompt overhead 로 실제 입력이 한도를 넘는 경계
          가 남음 (codex PR #26 review #1).

        ### 예산 비교 정책의 단일 지점 — `TokenBudget.max_chars()`

        char/token 변환 상수는 도메인의 `TokenBudget.chars_per_token()` 로 캡슐화돼
        있어 use case 가 별도 상수를 두면 두 정책이 어긋날 위험. `self._budget` 인스턴스
        의 `fits()` 를 직접 사용 — `FileDumpCollector` 와 동일 기준 (codex PR #26
        review #1 권장 + gemini PR #26 권고).
        """
        diff_text = assemble_pr_diff(pr)
        if not diff_text:
            logger.warning(
                "budget exceeded for %s#%d and no diff available — posting notice",
                pr.repo.full_name,
                pr.number,
            )
            self._github.post_comment(pr, _budget_exceeded_message(pr, dump))
            return None

        # 실제 모델 입력 = build_diff_prompt 결과. diff_text 만 검사하면 SYSTEM_RULES +
        # DIFF_MODE_NOTICE + PR 메타 overhead 로 한도 초과 가능 (codex PR #26 review #1).
        prompt_chars = len(build_diff_prompt(pr, diff_text))
        if not self._budget.fits(prompt_chars):
            logger.warning(
                "budget exceeded for %s#%d and diff prompt also too large "
                "(prompt_chars=%d > max_chars=%d, diff_chars=%d) — posting notice",
                pr.repo.full_name,
                pr.number,
                prompt_chars,
                self._budget.max_chars(),
                len(diff_text),
            )
            self._github.post_comment(pr, _budget_exceeded_message(pr, dump))
            return None

        logger.warning(
            "budget exceeded for %s#%d — falling back to diff-only review "
            "(prompt_chars=%d, diff_chars=%d, file_patches=%d)",
            pr.repo.full_name,
            pr.number,
            prompt_chars,
            len(diff_text),
            len(pr.file_patches),
        )
        return self._engine.review_diff(pr, diff_text)


def _changed_missing(pr: PullRequest, dump: FileDump) -> bool:
    included = {e.path for e in dump.entries}
    return any(cf not in included for cf in pr.changed_files)


def _budget_exceeded_message(pr: PullRequest, dump: FileDump) -> str:
    budget = dump.budget
    max_tokens = budget.max_tokens if budget is not None else 0
    included = len(dump.entries)
    excluded = len(dump.excluded)
    return (
        "⚠️ **Gemini Review — 컨텍스트 예산 초과**\n\n"
        f"본 저장소의 전체 코드 크기가 설정된 입력 한도(`GEMINI_MAX_INPUT_TOKENS={max_tokens}`)"
        "를 초과하여 리뷰를 수행하지 않았습니다.\n\n"
        f"- 포함된 파일: {included}개\n"
        f"- 제외된 파일: {excluded}개 (변경 파일 일부 포함)\n\n"
        "다음 중 하나를 조치해 주세요:\n"
        "1. PR 범위를 줄여 변경 파일이 컨텍스트에 들어가도록 분할\n"
        "2. `.gemini-reviewignore` 등으로 제외 규칙 확장\n"
        "3. `GEMINI_MAX_INPUT_TOKENS` 값을 상향 조정 (모델 컨텍스트 허용 범위 내)\n"
    )
