from pathlib import Path
from typing import Protocol

from gemini_review.domain import PullRequest


class RepoFetcher(Protocol):
    def checkout(self, pr: PullRequest, installation_token: str) -> Path:
        """Fetch the repo at the PR's head SHA and return the local path."""
        ...
