#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.payment_certification.post_launch import PostLaunchEvidence, certify_post_launch


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only PAY-23 post-launch financial certifier"
    )
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()

    data = json.loads(args.evidence.read_text(encoding="utf-8"))
    evidence = PostLaunchEvidence.from_dict(data)
    decision = certify_post_launch(evidence)

    print(
        json.dumps(
            {
                "phase": "PAY-23",
                "certified": decision.certified,
                "failures": list(decision.failures),
                "window_start": evidence.window_start.isoformat(),
                "window_end": evidence.window_end.isoformat(),
                "environment": evidence.environment,
                "activation_stage": evidence.activation_stage,
                "evidence_manifest_sha256": evidence.evidence_manifest_sha256,
            },
            sort_keys=True,
        )
    )

    if not decision.certified:
        return 2

    print("PAY23_ENTERPRISE_PAYMENT_SYSTEM=CERTIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
