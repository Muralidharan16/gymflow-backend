#!/usr/bin/env python3
"""Static contract tests for the immutable P10-B budget freeze."""

from __future__ import annotations

import copy
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFIER_PATH = ROOT / "scripts/ci/verify_p10_performance_budgets.py"
BUDGET_PATH = ROOT / "docs/architecture/p10_performance_budgets.v1.json"

spec = importlib.util.spec_from_file_location("p10_budget_verifier", VERIFIER_PATH)
assert spec and spec.loader
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


class P10BPerformanceBudgetContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))

    def test_frozen_document_validates(self) -> None:
        self.assertEqual(verifier.validate_document(self.document), [])
        self.assertEqual(
            verifier.canonical_digest(self.document),
            verifier.EXPECTED_DIGEST,
        )

    def test_all_calibrations_are_same_exact_candidate(self) -> None:
        source = self.document["source_candidate_sha"]
        for calibration in self.document["calibrations"].values():
            self.assertEqual(calibration["candidate_sha"], source)

    def test_budget_loosening_is_rejected(self) -> None:
        mutated = copy.deepcopy(self.document)
        mutated["budgets"]["representative_http"]["min_throughput_rps"] = 44.0
        errors = verifier.validate_document(mutated)
        self.assertTrue(any("frozen numeric budgets changed" in error for error in errors))


    def test_soak_growth_budgets_are_explicit_and_frozen(self) -> None:
        soak = self.document["budgets"]["soak_stability"]
        self.assertEqual(soak["duration_seconds"], 300)
        self.assertEqual(soak["max_rss_growth_bytes"], 33554432)
        self.assertEqual(soak["max_db_connection_growth"], 8)
        self.assertEqual(soak["max_db_connections"], 32)
        self.assertEqual(soak["max_worker_broker_depth"], 0)
        self.assertEqual(soak["max_throughput_degradation_ratio"], 0.15)
        self.assertEqual(soak["max_overall_p95_growth_ratio"], 1.2)
        self.assertEqual(soak["max_write_p95_growth_ratio"], 1.2)

        mutated = copy.deepcopy(self.document)
        mutated["budgets"]["soak_stability"]["max_rss_growth_bytes"] += 1
        errors = verifier.validate_document(mutated)
        self.assertTrue(any("frozen numeric budgets changed" in error for error in errors))

    def test_provider_effects_remain_fail_closed(self) -> None:
        self.assertFalse(self.document["scope"]["live_provider_credentials"])
        self.assertEqual(
            self.document["scope"]["refund_provider_execution"],
            "DEFERRED_FAIL_CLOSED",
        )
        self.assertEqual(
            self.document["calibrations"]["durable_queue"]["observed"]["external_provider_effects"],
            0,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
