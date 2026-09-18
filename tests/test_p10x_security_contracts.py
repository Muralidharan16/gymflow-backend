from pathlib import Path
import json
ROOT=Path(__file__).resolve().parents[1]
def test_p10x_scanners_are_exactly_pinned_and_suppressions_machine_readable():
    s=json.loads((ROOT/"security/p10x_scanners.v1.json").read_text())
    assert s["tools"]["pip-audit"]["version"]=="2.10.1"
    assert s["tools"]["bandit"]["version"]=="1.9.4"
    assert len(s["tools"]["trivy"]["sha256"])==64 and len(s["tools"]["gitleaks"]["sha256"])==64
    sup=json.loads((ROOT/"security/p10x_scan_suppressions.v1.json").read_text())
    assert sup["schema_version"]==1
    assert len(sup["suppressions"])==2
    for entry in sup["suppressions"]:
        assert entry["scanner"]=="gitleaks"
        assert entry["rule_or_advisory_id"] in {"generic-api-key","jwt"}
        assert len(entry["exact_scope"]["sorted_fingerprint_sha256"])==64
        assert entry["exact_scope"]["finding_count"] >= entry["exact_scope"]["unique_fingerprint_count"] > 0
        for field in ("justification","owner","reviewed_at","expires_at"):
            assert entry[field]
def test_p10x_workflow_has_all_required_security_surfaces_and_no_ignore_unfixed():
    w=(ROOT/".github/workflows/p10x-security-scans.yml").read_text()
    for token in ("pip-audit","bandit","trivy","gitleaks","cyclonedx","P10_SECURITY_SCANS=PASS","P10_SECRETS_CONFIG_FAIL_CLOSED=PASS"): assert token in w
    assert "--ignore-unfixed" not in w
    assert ".trivyignore" not in w
    assert "--exit-code 0" not in w
    assert "scripts/ci/p10x_review_gitleaks.py" in w
    assert "P10X_GITLEAKS_EXACT_REVIEW=PASS" in w


def test_gitleaks_review_is_exact_digest_scoped_and_expiring():
    review=(ROOT/"scripts/ci/p10x_review_gitleaks.py").read_text()
    for token in (
        "sorted_fingerprint_sha256",
        "unique_fingerprint_count",
        "unexpected_rules",
        "stale Gitleaks suppressions",
        "suppression expired",
        'finding.get("Secret") != "REDACTED"',
    ):
        assert token in review
def test_p10x_config_probe_has_missing_empty_debug_and_provider_failclosed_cases():
    p=(ROOT/"scripts/ci/p10x_config_fail_closed.py").read_text()
    for token in ("missing SECRET_KEY","empty SECRET_KEY","empty AWS_ACCESS_KEY_ID","empty AWS_SECRET_ACCESS_KEY","production debug","DEFERRED_FAIL_CLOSED"): assert token in p



def test_jwt_stack_excludes_unfixed_python_ecdsa_chain():
    lock = (ROOT / "requirements-test.lock").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    security = (ROOT / "app/core/security.py").read_text(encoding="utf-8")
    calibration = (ROOT / "scripts/ci/p10b_load_calibration.py").read_text(encoding="utf-8")

    assert "PyJWT==2.14.0" in lock
    assert "PyJWT>=2.14,<3" in requirements
    for forbidden in (
        "python-jose",
        "ecdsa==",
        "rsa==",
        "pyasn1==",
        "from jose",
    ):
        assert forbidden not in lock
        assert forbidden not in requirements
        assert forbidden not in security
        assert forbidden not in calibration
    assert "from jwt.exceptions import ExpiredSignatureError, PyJWTError" in security
