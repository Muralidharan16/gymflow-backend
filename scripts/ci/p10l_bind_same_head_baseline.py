#!/usr/bin/env python3
"""Bind P10-L to the canonical same-head P10-B production-load artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import time
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path, PurePosixPath

from github_artifact_transport import download_github_artifact

WORKFLOW_PATH = ".github/workflows/p10b-baseline-calibration.yml"
REQUIRED_FILES = (
    "http-calibration.json",
    "resource-calibration.json",
    "postgres-after.txt",
    "redis-stats.txt",
    "redis-ping.txt",
    "decision.json",
    "decision.sha256",
)


def _get_json(url: str, token: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if not members:
        raise RuntimeError("P10-B evidence archive is empty")
    for member in members:
        path = PurePosixPath(member.filename)
        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError(f"unsafe artifact path: {member.filename!r}")
    return members


def _locate(extracted: Path, name: str) -> Path:
    matches = [p for p in extracted.rglob(name) if p.is_file()]
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one {name}, found {len(matches)}")
    return matches[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--event", choices=("push", "pull_request"), required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-wait-seconds", type=int, default=1200)
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN", "")
    if not token:
        raise SystemExit("GH_TOKEN is required")
    if len(args.sha) != 40:
        raise SystemExit("exact 40-character candidate SHA is required")

    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    deadline = time.monotonic() + args.max_wait_seconds
    selected: dict | None = None
    last_state = "not found"

    while time.monotonic() < deadline:
        payload = _get_json(
            f"{api}/repos/{args.repo}/actions/runs"
            f"?head_sha={args.sha}&event={args.event}&per_page=100",
            token,
        )
        candidates = [
            run
            for run in payload.get("workflow_runs", [])
            if run.get("path") == WORKFLOW_PATH
            and run.get("head_sha") == args.sha
            and run.get("event") == args.event
        ]
        if candidates:
            selected = max(candidates, key=lambda item: int(item["id"]))
            last_state = (
                f"id={selected['id']} status={selected['status']} "
                f"conclusion={selected.get('conclusion')}"
            )
            if selected["status"] == "completed":
                break
        time.sleep(10)

    if selected is None or selected["status"] != "completed":
        raise RuntimeError(f"same-head P10-B calibration did not complete: {last_state}")
    if selected.get("conclusion") != "success":
        raise RuntimeError(
            f"same-head P10-B calibration is not successful: {last_state}"
        )

    artifact_payload = _get_json(selected["artifacts_url"] + "?per_page=100", token)
    expected_name = f"p10b-calibration-{args.sha}"
    artifacts = [
        item
        for item in artifact_payload.get("artifacts", [])
        if item.get("name") == expected_name and not item.get("expired")
    ]
    if len(artifacts) != 1:
        raise RuntimeError(
            f"expected one retained artifact {expected_name!r}, found {len(artifacts)}"
        )
    artifact = artifacts[0]
    archive_bytes = download_github_artifact(artifact["archive_download_url"], token)
    archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    extracted = output / "_p10b_artifact"
    if extracted.exists():
        shutil.rmtree(extracted)
    extracted.mkdir(parents=True)

    with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
        members = _safe_members(archive)
        archive.extractall(extracted, members=members)

    bound_files: dict[str, str] = {}
    for name in REQUIRED_FILES:
        source = _locate(extracted, name)
        target = output / name
        shutil.copy2(source, target)
        bound_files[name] = hashlib.sha256(target.read_bytes()).hexdigest()

    decision = json.loads((output / "decision.json").read_text(encoding="utf-8"))
    if decision.get("candidate_sha") != args.sha:
        raise RuntimeError(
            "P10-B artifact candidate SHA mismatch: "
            f"{decision.get('candidate_sha')} != {args.sha}"
        )
    if decision.get("decision") != "CALIBRATION_PASS":
        raise RuntimeError("P10-B artifact decision is not CALIBRATION_PASS")
    if not decision.get("production_container"):
        raise RuntimeError("P10-B artifact was not produced by the production container")
    if not decision.get("real_redis"):
        raise RuntimeError("P10-B artifact did not use real Redis")
    if int(decision.get("postgresql_major", 0)) != 16:
        raise RuntimeError("P10-B artifact did not use PostgreSQL 16")

    provenance = {
        "schema_version": 1,
        "phase": "P10-L",
        "candidate_sha": args.sha,
        "event": args.event,
        "source_workflow": WORKFLOW_PATH,
        "source_run_id": selected["id"],
        "source_run_name": selected["name"],
        "source_run_conclusion": selected["conclusion"],
        "artifact_id": artifact["id"],
        "artifact_name": artifact["name"],
        "artifact_archive_sha256": archive_sha256,
        "github_artifact_digest": artifact.get("digest"),
        "bound_file_sha256": bound_files,
        "decision": "BOUND",
    }
    (output / "p10b-provenance.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(provenance, indent=2, sort_keys=True))
    print("P10L_SAME_HEAD_BASELINE_BOUND=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
