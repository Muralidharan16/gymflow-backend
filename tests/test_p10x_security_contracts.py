from pathlib import Path
import json
ROOT=Path(__file__).resolve().parents[1]
def test_p10x_scanners_are_exactly_pinned_and_suppressions_machine_readable():
    s=json.loads((ROOT/"security/p10x_scanners.v1.json").read_text())
    assert s["tools"]["pip-audit"]["version"]=="2.10.1"
    assert s["tools"]["bandit"]["version"]=="1.9.4"
    assert len(s["tools"]["trivy"]["sha256"])==64 and len(s["tools"]["gitleaks"]["sha256"])==64
    sup=json.loads((ROOT/"security/p10x_scan_suppressions.v1.json").read_text())
    assert sup["schema_version"]==1 and sup["suppressions"]==[]
def test_p10x_workflow_has_all_required_security_surfaces_and_no_ignore_unfixed():
    w=(ROOT/".github/workflows/p10x-security-scans.yml").read_text()
    for token in ("pip-audit","bandit","trivy","gitleaks","cyclonedx","P10_SECURITY_SCANS=PASS","P10_SECRETS_CONFIG_FAIL_CLOSED=PASS"): assert token in w
    assert "--ignore-unfixed" not in w
    assert ".trivyignore" not in w
def test_p10x_config_probe_has_missing_empty_debug_and_provider_failclosed_cases():
    p=(ROOT/"scripts/ci/p10x_config_fail_closed.py").read_text()
    for token in ("missing SECRET_KEY","empty SECRET_KEY","empty AWS_ACCESS_KEY_ID","empty AWS_SECRET_ACCESS_KEY","production debug","DEFERRED_FAIL_CLOSED"): assert token in p
