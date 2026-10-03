"""Trusted release identity implementations for PAY-24 terminal operations."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.payment_activation.authority import (
    MeasuredReleaseIdentity,
    TrustedReleaseIdentityUnavailable,
)


_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class GitWorktreeReleaseIdentityProvider:
    """Measure the exact clean Git HEAD from a deployment-owned checkout.

    The deployed SHA is never accepted as a caller argument. The provider reads
    it directly from Git and refuses dirty/untracked worktrees, detached paths
    outside the requested root, malformed SHA output, command failures, or a
    non-canonical Git binary.
    """

    repo_root: Path
    git_binary: str = "/usr/bin/git"

    def measure(self) -> MeasuredReleaseIdentity:
        root = self.repo_root.expanduser().resolve()
        git = Path(self.git_binary)

        if not root.is_dir():
            raise TrustedReleaseIdentityUnavailable(
                "trusted Git release root is unavailable"
            )
        if not git.is_absolute() or not git.is_file():
            raise TrustedReleaseIdentityUnavailable(
                "trusted Git executable is unavailable"
            )

        top = self._run(root, "rev-parse", "--show-toplevel")
        try:
            canonical_top = Path(top).resolve()
        except (OSError, RuntimeError) as exc:
            raise TrustedReleaseIdentityUnavailable(
                "trusted Git release root is malformed"
            ) from exc
        if canonical_top != root:
            raise TrustedReleaseIdentityUnavailable(
                "trusted Git release root does not match repository top-level"
            )

        head = self._run(root, "rev-parse", "--verify", "HEAD")
        if not _SHA.fullmatch(head):
            raise TrustedReleaseIdentityUnavailable(
                "trusted Git release SHA is malformed"
            )

        status = self._run(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=normal",
        )
        if status:
            raise TrustedReleaseIdentityUnavailable(
                "trusted Git release worktree is not clean"
            )

        return MeasuredReleaseIdentity(
            deployed_sha=head,
            measured_by="git-worktree-head-v1",
            measured_at=datetime.now(UTC),
        )

    def _run(self, root: Path, *args: str) -> str:
        try:
            completed = subprocess.run(
                [self.git_binary, "-C", str(root), *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
                env={
                    "HOME": str(root),
                    "LC_ALL": "C",
                    "LANG": "C",
                    "PATH": "/usr/bin:/bin",
                },
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise TrustedReleaseIdentityUnavailable(
                "trusted Git release measurement failed"
            ) from exc

        if completed.stderr.strip():
            # Git warnings can change across versions and are not authority.
            # Refuse noisy measurements rather than interpreting them.
            raise TrustedReleaseIdentityUnavailable(
                "trusted Git release measurement was noisy"
            )
        return completed.stdout.strip()
