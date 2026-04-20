from typing import Protocol

from gemini_review.domain import FileDump, PullRequest, ReviewResult


class ReviewEngine(Protocol):
    def review(self, pr: PullRequest, dump: FileDump) -> ReviewResult: ...
