"""Trusted release identity implementations for PAY-24 terminal operations."""

from __future__ import annotations

import json
import os
import re
import stat
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


@dataclass(frozen=True, slots=True)
class RootOwnedReleaseAttestationProvider:
    """Read deployment-owned exact-SHA metadata from a protected file."""

    attestation_path: Path

    def measure(self) -> MeasuredReleaseIdentity:
        path = self.attestation_path.expanduser()
        if not path.is_absolute():
            raise TrustedReleaseIdentityUnavailable(
                "release attestation path must be absolute"
            )
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise TrustedReleaseIdentityUnavailable(
                "release attestation file is unavailable"
            ) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise TrustedReleaseIdentityUnavailable(
                "release attestation must be a regular non-symlink file"
            )
        if info.st_uid != 0:
            raise TrustedReleaseIdentityUnavailable(
                "release attestation must be owned by root"
            )
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise TrustedReleaseIdentityUnavailable(
                "release attestation must not be group/other writable"
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TrustedReleaseIdentityUnavailable(
                "release attestation is unreadable"
            ) from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise TrustedReleaseIdentityUnavailable(
                "release attestation schema is invalid"
            )
        sha = payload.get("deployed_sha")
        measured_by = payload.get("measured_by")
        measured_at_raw = payload.get("measured_at")
        if (
            not isinstance(sha, str)
            or not _SHA.fullmatch(sha)
            or not isinstance(measured_by, str)
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:/-]{2,127}",
                measured_by,
            )
            or not isinstance(measured_at_raw, str)
        ):
            raise TrustedReleaseIdentityUnavailable(
                "release attestation identity is malformed"
            )
        try:
            measured_at = datetime.fromisoformat(
                measured_at_raw.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise TrustedReleaseIdentityUnavailable(
                "release attestation timestamp is malformed"
            ) from exc
        if measured_at.tzinfo is None or measured_at.utcoffset() is None:
            raise TrustedReleaseIdentityUnavailable(
                "release attestation timestamp must be timezone-aware"
            )
        return MeasuredReleaseIdentity(
            deployed_sha=sha,
            measured_by=measured_by,
            measured_at=measured_at,
        )
