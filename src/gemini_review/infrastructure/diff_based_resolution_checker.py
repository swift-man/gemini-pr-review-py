"""DiffBasedResolutionChecker — Layer E 후속 수정 추적 + 대댓글 게시.

사용자 요청 (2026-04): "라인 코멘트를 일방적으로 다는 것으로 끝나는 것이 아닌, 후속
수정사항이 생기면 본인이 단 코멘트에 대댓글로 수정여부 확인". 이 layer 가 그 기능.

### 동작

1. 본 봇이 이전에 PR 에 게시한 라인 고정 코멘트 목록을 조회 (`list_self_review_comments`).
2. 각 top-level 코멘트 (대댓글 자신은 제외) 에 대해:
   - 비-차단급 (Minor / Suggestion) 은 노이즈 회피 위해 skip.
   - 본 봇이 이미 대댓글을 단 코멘트는 skip (중복 reply 회피).
   - 코멘트의 `commit_id` 가 현재 `pr.head_sha` 와 같으면 skip (변경 가능성 0).
   - 그 외: 로컬 git checkout 에서 `commit_id` 시점과 `head_sha` 시점의 해당 라인
     본문을 비교. 다르면 "라인이 변경됨" → 부모 코멘트 thread 에 follow-up 대댓글.

### Diff-only 정책 (v1)

라인 본문이 바뀐 사실만 보고 "수정 의도가 맞는지" 는 메인테이너가 판단하도록 안내.
모델로 "해결됐나" 를 판정하려면 추가 호출 비용 + 환각 위험. v1 은 단순 diff 비교만:

> 📌 라인이 변경되었습니다 ([prior_sha:7] → [head_sha:7])
> **이전:** `<old_content>`
> **현재:** `<new_content>`
> 의도된 수정인지 확인 부탁드립니다.

### Layer D 와의 분담

- Layer D (`CrossPrFindingDeduper`): 새 push 의 finding 이 이전과 동일 (메인테이너
  무시 신호) → [Suggestion] 강등.
- Layer E (이 모듈): 이전 push 의 코멘트 라인이 새 push 에서 수정 (메인테이너 처리
  신호) → 부모 코멘트에 follow-up 대댓글.

두 layer 가 보완적: dedup 은 "무시된 finding", follow-up 은 "처리된 finding". 정합성:
같은 push 에서 한쪽은 강등, 다른 쪽은 reply 가 가능하지만 두 신호 모두 메인테이너
판단을 돕는 정보라 모순 아님.

### Graceful degrade

- list_self_review_comments 실패 → WARN + 조용히 종료 (리뷰 게시는 이미 끝남)
- 한 코멘트의 git show 실패 (commit 로컬 X, force-push 후 unreachable 등) → 그 코멘트
  skip, 다음 코멘트 진행
- 한 코멘트의 reply 게시 실패 (404, rate-limit 등) → WARN + 다음 코멘트 진행
"""
import logging
import subprocess
from pathlib import Path

from gemini_review.domain import PostedReviewComment, PullRequest
from gemini_review.infrastructure.source_grounded_finding_verifier import (
    _BLOCKING_SEVERITIES,
    _SEVERITY_PREFIX_HEAD,
)
from gemini_review.interfaces import GitHubClient

logger = logging.getLogger(__name__)


class DiffBasedResolutionChecker:
    """이전 코멘트 라인이 수정된 케이스에 대해 부모 코멘트 thread 에 follow-up 대댓글."""

    def __init__(self, github: GitHubClient) -> None:
        self._github = github

    def check_resolutions(self, pr: PullRequest, repo_root: Path) -> None:
        try:
            all_comments = self._github.list_self_review_comments(pr)
        except Exception as exc:  # noqa: BLE001 — graceful degrade
            logger.warning(
                "list_self_review_comments failed for %s#%d (%s); "
                "skipping resolution check",
                pr.repo.full_name,
                pr.number,
                exc,
            )
            return

        if not all_comments:
            return

        # 본 봇이 이미 대댓글을 단 부모 id 집합 — 중복 reply 회피.
        # in_reply_to_id 가 set 된 코멘트는 자체가 reply (본 봇이 단 것). 그 부모를
        # 추적해 두면 이번 push 에서 재 reply 발동 시 skip 가능.
        already_replied_to: set[int] = {
            c.in_reply_to_id for c in all_comments if c.in_reply_to_id is not None
        }

        for comment in all_comments:
            if comment.in_reply_to_id is not None:
                # 본인이 단 reply — top-level 검사 대상 아님
                continue
            if comment.comment_id in already_replied_to:
                # 이미 follow-up reply 단 코멘트 — 같은 변경에 두 번 reply 방지
                continue
            self._maybe_reply(pr, repo_root, comment)

    def _maybe_reply(
        self, pr: PullRequest, repo_root: Path, comment: PostedReviewComment
    ) -> None:
        # 비-차단급은 follow-up 대상 아님 — Minor/Suggestion 은 메인테이너가 무시해도
        # 정상이라 "수정됐나?" reply 가 노이즈가 됨.
        head = _SEVERITY_PREFIX_HEAD.match(comment.body)
        if head is None or head.group(1) not in _BLOCKING_SEVERITIES:
            return

        # **anchor SHA** 비교: original_commit_id 가 head_sha 와 같으면 코멘트 anchor
        # 시점이 곧 현재 head 라는 뜻 — 시간상 후속 push 가 없었음. commit_id 로 비교
        # 하면 GitHub 가 line shift 추적으로 commit_id 를 head 로 갱신해 둔 케이스에선
        # 항상 같은 sha 로 보여 모든 reply 가 차단되는 회귀 (gemini PR #28 review #1).
        if comment.original_commit_id == pr.head_sha:
            return

        # **prior 측 = original anchor**: GitHub 가 추적해 갱신하기 전의 SHA / line.
        # **current 측 = (commit_id, line)**: GitHub 가 head 시점으로 추적 갱신한 위치.
        # GitHub 가 line shift 를 잘 따라갔다면 prior_content == head_content (라인이
        # 옮겨졌을 뿐 본문 같음) → no reply. 진짜 본문이 바뀌었다면 다름 → reply.
        prior_line = _read_line_at_commit(
            repo_root, comment.original_commit_id, comment.path, comment.original_line
        )
        head_line = _read_line_at_commit(
            repo_root, comment.commit_id, comment.path, comment.line
        )
        if prior_line is None or head_line is None:
            # 한쪽이라도 못 읽으면 (commit unreachable, 파일 사라짐, 라인 범위 밖)
            # 비교 불가 → 안전한 skip. false reply 보다 reply 안 하는 게 나음.
            return
        # 양끝 공백 strip 후 비교 (gemini PR #28 review #5): 들여쓰기/trailing space 만
        # 바뀐 라인은 메인테이너 처리 신호 X — formatter 자동 정리 등으로 noise 만 됨.
        # 진짜 본문이 바뀐 경우만 reply. strip 결과를 reply 본문엔 안 쓰고 비교만 — 본문
        # 인용은 원본 그대로 유지해 메인테이너가 정확한 변화를 보도록.
        if prior_line.strip() == head_line.strip():
            # 본문 strip 결과가 같음 → 들여쓰기/trailing space 만 다름 → reply 안 함
            return

        # 본문/로그 SHA = head_line 을 실제 읽은 SHA (coderabbitai PR #28 review #4 +
        # gemini round 1 line 132). GitHub 가 아직 comment.commit_id 를 pr.head_sha 로
        # 추적 갱신하지 못한 순간 "표시 SHA" ≠ "비교 SHA" 가 되어 메인테이너 혼란.
        # comment.commit_id 로 일치시키면 본문에 보여준 SHA 의 라인을 그대로 비교한 결과
        # 라는 invariant 가 명확해진다.
        body = _build_resolution_reply(
            prior_sha=comment.original_commit_id,
            head_sha=comment.commit_id,
            prior_line=prior_line,
            head_line=head_line,
        )
        try:
            self._github.reply_to_review_comment(pr, comment.comment_id, body)
        except Exception as exc:  # noqa: BLE001 — 한 reply 실패가 다음 reply 막지 않음
            logger.warning(
                "reply_to_review_comment failed for %s#%d cid=%d (%s); skipping",
                pr.repo.full_name,
                pr.number,
                comment.comment_id,
                exc,
            )
            return
        logger.info(
            "follow-up reply posted for %s#%d cid=%d (line %s:%d changed %s..%s)",
            pr.repo.full_name,
            pr.number,
            comment.comment_id,
            comment.path,
            comment.original_line,
            comment.original_commit_id[:7],
            comment.commit_id[:7],
        )


def _read_line_at_commit(
    repo_root: Path, commit_sha: str, path: str, line: int
) -> str | None:
    """`git show {sha}:{path}` 의 1-based `line` 번 라인을 반환. 실패는 None.

    실패 케이스 (graceful degrade — 모두 None 반환):
    - commit 이 로컬에 없음 (force-push 후 unreachable, 또는 fetch 안 된 경우)
    - 파일이 그 commit 에 없음 (해당 push 에서 이름이 달랐음 등)
    - 라인 번호가 그 commit 의 파일 크기 범위 밖
    - subprocess 실패 (timeout, OSError 등)
    - 디코딩 실패 — `errors="replace"` 로 복구 (gemini PR #28 review #2): UTF-8 로
      디코딩 불가능한 바이너리 파일이나 다른 인코딩의 텍스트가 들어와도
      `UnicodeDecodeError` (ValueError 하위) 로 Layer E 전체가 터지면 안 됨.
      대체 문자(�) 로 디코딩한 결과를 그대로 비교 — 두 commit 의 같은 라인이라면
      대체 결과도 같아 비교는 의미 있는 결과를 낸다.
    """
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(repo_root), "show", f"{commit_sha}:{path}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    lines = result.stdout.splitlines()
    if line <= 0 or line > len(lines):
        return None
    return lines[line - 1]


def _build_resolution_reply(
    *, prior_sha: str, head_sha: str, prior_line: str, head_line: str
) -> str:
    """대댓글 본문 — diff-only 표기. 모델 판정 없이 두 라인 본문만 보여줌.

    "✅ 해결됨" 같은 confident 표기는 의도적으로 회피. 라인이 바뀐 것은 사실이지만
    원래 finding 의 의도와 일치하는 수정인지는 메인테이너가 판단해야 함. 대댓글이
    "확인 부탁드립니다" 톤으로 끝나는 게 v1 의 design contract.

    ### 라인 본문 렌더링 — 4-space indent 코드 블록 (gemini PR #28 review #4)

    inline backtick (`` `{line}` ``) 으로 감싸면 라인 본문에 backtick 이 포함된 경우
    (예: 마크다운 파일, 백틱 사용한 docstring 등) markdown 이 깨짐. 4-space indent
    code block 은 fence 와 무관하게 안전하고 어떤 backtick 개수의 본문도 그대로 표시.
    """
    return (
        f"📌 라인이 변경되었습니다 (`{prior_sha[:7]}` → `{head_sha[:7]}`).\n\n"
        f"**이전:**\n\n{_indent_code(prior_line)}\n\n"
        f"**현재:**\n\n{_indent_code(head_line)}\n\n"
        "의도된 수정인지 — 원래 지적의 의도와 일치하는지 확인 부탁드립니다."
    )


def _indent_code(line: str) -> str:
    """라인 본문을 4-space indent code block 으로 감싸 markdown safe 하게 렌더.

    빈 라인은 빈 indent 줄 하나로 보존 — markdown 렌더러가 코드 블록을 깨뜨리지 않도록.
    """
    return "    " + line if line else "    "
