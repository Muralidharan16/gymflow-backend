"""
tests/platform_billing/test_architecture.py
============================================
Architecture guardrail tests for Platform Billing Phase 0.

These tests enforce V2 constitutional boundaries and V3.1 execution
constraints. Every assertion maps to an explicit V2/V3.1 requirement.

Phase 0 scope:
    - Define and verify forbidden import boundaries
    - Verify runtime policy singleton is the only source of truth
    - Detect unauthorized constant duplication
    - Verify no database tables, no provider integration
    - Verify policy files have valid schemas and required fields
    - Verify document checksums
"""

from __future__ import annotations

import ast
import hashlib
import re
import sys
from pathlib import Path

import pytest
import yaml


# ──────────────────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[2]
PLATFORM_BILLING_ROOT = REPO_ROOT / "app" / "platform_billing"
POLICIES_DATA = PLATFORM_BILLING_ROOT / "policies" / "data"
DOCS_DIR = REPO_ROOT / "docs" / "architecture"


# ──────────────────────────────────────────────────────────────────────────
# 1. Forbidden import boundaries
# ──────────────────────────────────────────────────────────────────────────

FACILITY_COMMERCE_MODULES = frozenset(
    {
        "app.models.subscription",
        "app.models.payment",
        "app.models.membership_plan",
        "app.models.member_subscription_v2",
        "app.models.trial",
        "app.models.enums",
        "app.services.subscription_service",
        "app.services.payment_service",
        "app.services.member_subscription_v2_service",
        "app.services.plan_service",
        "app.services.invoice_service",
        "app.services.trial_service",
        "app.repositories.subscription_repo",
        "app.repositories.payment_repo",
        "app.repositories.member_subscription_v2_repo",
    }
)

FORBIDDEN_FOR_PLATFORM_BILLING = FACILITY_COMMERCE_MODULES | {
    "app.routers.subscriptions",
    "app.routers.member_subscriptions_v2",
    "app.routers.payments",
    "app.routers.membership_plans",
    "app.schemas.subscription",
    "app.schemas.payment",
    "app.schemas.member_subscription_v2",
    "app.schemas.membership_plan",
}

# V2 §1.3: Hard invariant — platform billing and member commerce never share records.
# V3.1 §1.1: Domain boundary — existing member-commerce modules never imported into platform_billing.

FACILITY_COMMERCE_SOURCE_FILES = (
    "app/models/subscription.py",
    "app/models/payment.py",
    "app/models/trial.py",
    "app/models/membership_plan.py",
    "app/models/member_subscription_v2.py",
    "app/models/gym.py",
    "app/services/subscription_service.py",
    "app/services/payment_service.py",
    "app/services/trial_service.py",
    "app/services/invoice_service.py",
    "app/services/gym_service.py",
    "app/services/member_subscription_v2_service.py",
    "app/services/plan_service.py",
    "app/routers/subscriptions.py",
    "app/routers/payments.py",
    "app/routers/member_subscriptions_v2.py",
    "app/routers/membership_plans.py",
    "app/schemas/subscription.py",
    "app/schemas/payment.py",
    "app/schemas/member_subscription_v2.py",
    "app/schemas/membership_plan.py",
)


def _collect_imports(file_path: Path) -> set[str]:
    source = file_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module)
    return imports


def _platform_billing_py_files() -> list[Path]:
    root = PLATFORM_BILLING_ROOT
    return sorted(root.rglob("*.py"))


def test_no_platform_billing_imports_facility_commerce():
    violations: list[str] = []
    for py_file in _platform_billing_py_files():
        if py_file.name == "__init__.py" and "tests" in str(py_file):
            continue
        file_imports = _collect_imports(py_file)
        for imp in file_imports:
            if imp in FORBIDDEN_FOR_PLATFORM_BILLING:
                rel = py_file.relative_to(REPO_ROOT)
                violations.append(f"{rel} imports forbidden module: {imp}")
    assert not violations, (
        "Platform Billing modules must not import facility-commerce modules. "
        "V2 §1.3: Hard invariant — domains never share records.\n"
        + "\n".join(violations)
    )


def test_no_facility_commerce_imports_platform_billing():
    violations: list[str] = []
    for rel_path in FACILITY_COMMERCE_SOURCE_FILES:
        py_file = REPO_ROOT / rel_path
        if not py_file.exists():
            continue
        file_imports = _collect_imports(py_file)
        for imp in file_imports:
            if imp.startswith("app.platform_billing"):
                violations.append(f"{rel_path} imports {imp}")
    assert not violations, (
        "Facility-commerce modules must not import platform_billing. "
        "V2 §1.3: Hard invariant — domains never share records.\n"
        + "\n".join(violations)
    )


# ──────────────────────────────────────────────────────────────────────────
# 2. Runtime policy is the single source of truth
# ──────────────────────────────────────────────────────────────────────────

# V3.1 §1.7: Numeric and boundary defaults live in one validated manifest.
# Services, workers, tests, and frontend contracts may not redefine them independently.
#
# The drift test scans ONLY platform_billing code for literal redefinitions of
# runtime policy values. Coincidental numeric matches in legacy code (e.g., the
# number 150 in TIER_LIMITS["pro"]["max_members"]=1500) are not considered drift.

RUNTIME_SEARCH_GLOBS = [
    "app/platform_billing/**/*.py",
    "tests/platform_billing/**/*.py",
]

# Variable-name-like patterns that suggest someone is redefining a policy value
RUNTIME_NAMED_PATTERNS = {
    "access_resolution_sync_timeout_ms",
    "sync_timeout_ms",
    "ACCESS_RESOLUTION_SYNC_TIMEOUT",
    "SYNC_TIMEOUT",
    "policy_day_seconds",
    "POLICY_DAY_SECONDS",
    "POLICY_DAY",
    "first_subscription_lock_namespace",
    "FIRST_SUBSCRIPTION_LOCK",
}

RUNTIME_VALUES_TO_GUARD = {150, 86400, 150000}


def _is_test_or_manifest(file_path: Path) -> bool:
    rel = str(file_path)
    return (
        "test_" in rel
        or "conftest" in rel
        or "policy_loader.py" in rel
        or "platform_billing_runtime_v1.yaml" in rel
    )


def test_no_duplicate_runtime_constants_in_platform_billing():
    issues: list[str] = []
    for glob_pattern in RUNTIME_SEARCH_GLOBS:
        for py_file in REPO_ROOT.glob(glob_pattern):
            if not py_file.is_file():
                continue
            if _is_test_or_manifest(py_file):
                continue
            lines = py_file.read_text(encoding="utf-8").splitlines()
            for lineno, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                    continue

                has_policy_name = any(
                    pattern in line for pattern in RUNTIME_NAMED_PATTERNS
                )
                has_guarded_value = any(
                    str(val) in line and f"_test_{val}" not in line
                    for val in RUNTIME_VALUES_TO_GUARD
                )

                if has_policy_name and has_guarded_value:
                    rel = py_file.relative_to(REPO_ROOT)
                    issues.append(
                        f"{rel}:{lineno} may redefine a runtime policy value. "
                        f"Use get_runtime_policy() from app.platform_billing.policies instead."
                    )

    assert not issues, (
        "Platform billing code must not redeclare runtime policy values. "
        "V3.1 §1.7: one validated configuration manifest.\n" + "\n".join(issues)
    )


def test_runtime_policy_loads_successfully():
    from app.platform_billing.policies.policy_loader import get_runtime_policy

    policy = get_runtime_policy()
    assert policy is not None
    assert policy.access_resolution_sync_timeout_ms == 150
    assert policy.policy_day_seconds == 86400
    assert policy.provider_mapping_environment_match == "exact"


# ──────────────────────────────────────────────────────────────────────────
# 3. Package structure correctness
# ──────────────────────────────────────────────────────────────────────────

REQUIRED_SUBPACKAGES = (
    "api",
    "domain",
    "models",
    "repositories",
    "services",
    "providers",
    "policies",
    "tasks",
    "observability",
)


def test_platform_billing_package_structure():
    root = PLATFORM_BILLING_ROOT
    assert root.is_dir()
    for sub in REQUIRED_SUBPACKAGES:
        sub_dir = root / sub
        assert sub_dir.is_dir()
        init = sub_dir / "__init__.py"
        assert init.exists()


REQUIRED_DOMAIN_MODULES = (
    "enums.py",
    "errors.py",
    "money.py",
    "events.py",
    "commands.py",
)


def test_domain_module_structure():
    domain = PLATFORM_BILLING_ROOT / "domain"
    for mod in REQUIRED_DOMAIN_MODULES:
        assert (domain / mod).exists()


REQUIRED_POLICY_FILES = (
    "capabilities_v1.yaml",
    "entitlements_v1.yaml",
    "access_matrix_v1.yaml",
    "lifecycle_policies_v1.yaml",
    "platform_billing_runtime_v1.yaml",
)


def test_policy_data_files_present():
    for name in REQUIRED_POLICY_FILES:
        assert (POLICIES_DATA / name).exists()


# ──────────────────────────────────────────────────────────────────────────
# 4. No platform-billing database tables (Phase 0 prohibition)
# ──────────────────────────────────────────────────────────────────────────

def test_no_billing_database_tables_created():
    models_dir = PLATFORM_BILLING_ROOT / "models"
    for py_file in models_dir.glob("*.py"):
        if py_file.name == "__init__.py":
            continue
        source = py_file.read_text(encoding="utf-8")
        if "__tablename__" in source:
            rel = py_file.relative_to(REPO_ROOT)
            pytest.fail(
                f"{rel} defines __tablename__ — database tables are "
                f"prohibited in Phase 0 (V3.1 §25 explicit prohibition)"
            )


def test_no_alembic_migration_generated_for_phase_0():
    import subprocess
    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD", "--", "alembic/versions/"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    new_migrations = [
        line for line in result.stdout.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not new_migrations, (
        f"No Alembic migrations may be generated in Phase 0. Found: {new_migrations}"
    )


# ──────────────────────────────────────────────────────────────────────────
# 5. No provider integration (Phase 0 prohibition)
# ──────────────────────────────────────────────────────────────────────────

KNOWN_PROVIDER_MODULES = {
    "razorpay", "stripe", "cashfree", "paypal", "braintree",
    "square", "adyen", "payu", "instamojo", "ccavenue",
}


def test_no_provider_sdk_integration():
    violations: list[str] = []
    for py_file in _platform_billing_py_files():
        file_imports = _collect_imports(py_file)
        for imp in file_imports:
            module_top = imp.split(".")[0]
            if module_top in KNOWN_PROVIDER_MODULES:
                rel = py_file.relative_to(REPO_ROOT)
                violations.append(f"{rel} imports provider SDK: {imp}")
    assert not violations, (
        "No payment provider SDK may be integrated in Phase 0. "
        "V3.1 §25 explicit prohibition.\n" + "\n".join(violations)
    )


def test_no_webhook_routes_under_platform_billing():
    api_dir = PLATFORM_BILLING_ROOT / "api"
    for py_file in api_dir.glob("*.py"):
        if py_file.name == "__init__.py":
            continue
        source = py_file.read_text(encoding="utf-8")
        if "webhook" in source.lower() or "Webhook" in source:
            rel = py_file.relative_to(REPO_ROOT)
            pytest.fail(
                f"{rel} contains webhook references — webhook processing "
                f"is prohibited in Phase 0 (V3.1 §25)"
            )


# ──────────────────────────────────────────────────────────────────────────
# 6. Policy schema validation
# ──────────────────────────────────────────────────────────────────────────

VALID_ENUM_VALUES = {
    "operation_class": {"read", "modify_existing", "increase_capacity", "destructive", "financial", "recovery"},
    "value_type": {"boolean", "integer", "string", "json"},
    "enforcement_mode": {"hard", "soft", "metered", "informational"},
    "policy_type": {"trial", "dunning", "cancellation", "downgrade", "refund", "retention"},
    "status": {"draft", "published", "retired", "active"},
}


V3_1_CAPABILITY_KEYS = frozenset({
    "auth.session.refresh", "auth.logout", "security.manage_own_session", "support.contact",
    "platform_billing.view", "platform_billing.manage_account", "platform_billing.manage_payment_method",
    "platform_billing.change_plan", "platform_billing.cancel", "platform_billing.download_invoice",
    "data.export",
    "organization.view", "organization.update",
    "branches.view", "branches.create", "branches.update", "branches.change_status", "branches.delete_request",
    "branch_contacts.view", "branch_contacts.manage",
    "branch_hours.view", "branch_hours.manage",
    "staff.view", "staff.invite", "staff.update", "staff.revoke",
    "members.view", "members.create", "members.update", "members.deactivate",
    "membership_plans.view", "membership_plans.create", "membership_plans.update", "membership_plans.archive",
    "member_subscriptions.view", "member_subscriptions.create", "member_subscriptions.update", "member_subscriptions.cancel",
    "member_payments.view", "member_payments.record", "member_payments.refund",
    "attendance.view", "attendance.record",
    "reports.basic.view", "reports.advanced.view",
    "imports.create", "imports.view",
    "assets.view", "assets.manage",
    "internal.platform_billing.view", "internal.platform_billing.reconcile",
    "internal.platform_billing.issue_refund", "internal.platform_billing.apply_credit",
    "internal.platform_billing.override_access", "internal.platform_billing.manage_catalog",
    "internal.platform_billing.view_sensitive_audit",
})


def test_capability_registry_matches_v3_1():
    with open(POLICIES_DATA / "capabilities_v1.yaml") as f:
        data = yaml.safe_load(f)
    caps = data.get("capabilities", [])
    registered_keys = set()
    for cap in caps:
        assert "key" in cap, f"Capability missing 'key': {cap}"
        assert "description" in cap, f"Capability {cap.get('key')} missing 'description'"
        assert "operation_class" in cap, f"Capability {cap.get('key')} missing 'operation_class'"
        oc = cap["operation_class"]
        assert oc in VALID_ENUM_VALUES["operation_class"], f"Invalid operation_class {oc} in {cap['key']}"
        registered_keys.add(cap["key"])

    missing = V3_1_CAPABILITY_KEYS - registered_keys
    extra = registered_keys - V3_1_CAPABILITY_KEYS
    errors = []
    if missing:
        errors.append(f"Missing mandated V3.1 capabilities: {sorted(missing)}")
    if extra:
        errors.append(f"Extra capabilities not in V3.1 registry: {sorted(extra)}")
    assert not errors, "Capability registry must match V3.1 §10.1 initial definitions.\n" + "\n".join(errors)


REQUIRED_ACCESS_MODES = frozenset({"full", "limited_write", "read_only", "billing_only", "blocked"})

REQUIRED_LIFECYCLE_POLICIES = frozenset({"TRIAL-IN-V1", "DUNNING-IN-V1", "CANCEL-IN-V1", "DOWNGRADE-IN-V1", "REFUND-IN-V1"})


def test_access_matrix_has_all_modes():
    with open(POLICIES_DATA / "access_matrix_v1.yaml") as f:
        data = yaml.safe_load(f)
    matrix = data.get("matrix", {})
    assert set(matrix.keys()) == REQUIRED_ACCESS_MODES, (
        f"Access matrix must contain exactly modes {sorted(REQUIRED_ACCESS_MODES)}"
    )


def test_lifecycle_policies_complete():
    with open(POLICIES_DATA / "lifecycle_policies_v1.yaml") as f:
        data = yaml.safe_load(f)
    policies = data.get("policies", {})
    missing = REQUIRED_LIFECYCLE_POLICIES - set(policies.keys())
    assert not missing, f"Missing required lifecycle policies: {sorted(missing)}"
    for pcode in REQUIRED_LIFECYCLE_POLICIES:
        policy = policies[pcode]
        assert "policy_type" in policy, f"{pcode} missing policy_type"
        assert "version" in policy, f"{pcode} missing version"
        assert "params" in policy, f"{pcode} missing params"


REQUIRED_ENTITLEMENTS = frozenset({
    "limits.branches.active", "limits.members.active", "limits.staff.active",
    "limits.membership_plans.active", "limits.storage.bytes",
    "limits.monthly_messages", "limits.api_requests.monthly", "retention.audit_days",
    "features.multi_branch", "features.attendance", "features.member_subscriptions",
    "features.member_payments", "features.basic_reports", "features.advanced_reports",
    "features.custom_branding", "features.data_export", "features.api_access",
    "features.whatsapp", "features.priority_support",
})


def test_entitlement_registry_complete():
    with open(POLICIES_DATA / "entitlements_v1.yaml") as f:
        data = yaml.safe_load(f)
    ents = data.get("entitlements", [])
    registered = set()
    for ent in ents:
        assert "key" in ent, f"Entitlement missing 'key': {ent}"
        assert "value_type" in ent, f"Entitlement {ent.get('key')} missing 'value_type'"
        assert "enforcement_mode" in ent, f"Entitlement {ent.get('key')} missing 'enforcement_mode'"
        vt = ent["value_type"]
        assert vt in VALID_ENUM_VALUES["value_type"], f"Invalid value_type {vt} in {ent['key']}"
        em = ent["enforcement_mode"]
        assert em in VALID_ENUM_VALUES["enforcement_mode"], f"Invalid enforcement_mode {em} in {ent['key']}"
        registered.add(ent["key"])
    missing = REQUIRED_ENTITLEMENTS - registered
    assert not missing, f"Missing mandated entitlement keys: {sorted(missing)}"


# ──────────────────────────────────────────────────────────────────────────
# 7. Document checksums
# ──────────────────────────────────────────────────────────────────────────

EXPECTED_DOC_FILES = (
    "DOERS_PLATFORM_SUBSCRIPTION_CONSTITUTION_V2.md",
    "DOERS_PLATFORM_SUBSCRIPTION_V3_1_EXECUTION_SPEC.md",
    "PLATFORM_BILLING_SESSION_SECURITY_MIGRATION_PLAN.md",
)

CHECKSUM_MANIFEST_FILES = (
    "DOERS_PLATFORM_SUBSCRIPTION_CONSTITUTION_V2.md",
    "DOERS_PLATFORM_SUBSCRIPTION_V3_1_EXECUTION_SPEC.md",
)


def test_architecture_docs_exist():
    for name in EXPECTED_DOC_FILES:
        path = DOCS_DIR / name
        assert path.exists(), f"Missing required architecture document: {name}"


def test_architecture_docs_are_not_stubs():
    for name in EXPECTED_DOC_FILES:
        path = DOCS_DIR / name
        content = path.read_text(encoding="utf-8")
        assert len(content) > 1000, (
            f"{name} appears to be a stub ({len(content)} bytes). "
            f"It must contain the complete governing document."
        )
        stub_indicators = [
            "Refer to the authoritative",
            "doers-governance",
        ]
        for indicator in stub_indicators:
            assert indicator not in content, (
                f"{name} contains external reference '{indicator}' instead of full content"
            )


def _parse_sha256sums(manifest_path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    pattern = re.compile(r"^([0-9a-f]{64})  ([^\s/][^\n]*)$")

    for line_number, raw_line in enumerate(
        manifest_path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not raw_line.strip():
            continue
        match = pattern.fullmatch(raw_line)
        assert match, f"Malformed SHA256SUMS line {line_number}: {raw_line!r}"

        digest, filename = match.groups()
        assert filename not in entries, f"Duplicate SHA256SUMS entry: {filename}"
        entries[filename] = digest

    return entries


def test_architecture_doc_checksums_match_manifest():
    manifest_path = DOCS_DIR / "SHA256SUMS"
    assert manifest_path.exists(), "Missing required architecture checksum manifest"

    entries = _parse_sha256sums(manifest_path)
    expected_files = set(CHECKSUM_MANIFEST_FILES)
    manifest_files = set(entries)

    missing = expected_files - manifest_files
    extra = manifest_files - expected_files
    assert not missing, f"SHA256SUMS missing required entries: {sorted(missing)}"
    assert not extra, f"SHA256SUMS contains unexpected entries: {sorted(extra)}"

    mismatches: list[str] = []
    for filename, expected_digest in entries.items():
        path = DOCS_DIR / filename
        assert path.exists(), f"SHA256SUMS references missing file: {filename}"
        actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_digest != expected_digest:
            mismatches.append(
                f"{filename}: expected {expected_digest}, got {actual_digest}"
            )

    assert not mismatches, "Architecture document checksum mismatch:\n" + "\n".join(mismatches)


def test_document_checksums():
    import json
    checksums: dict[str, str] = {}
    for name in EXPECTED_DOC_FILES:
        path = DOCS_DIR / name
        if not path.exists():
            continue
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        checksums[name] = sha

    manifest_path = DOCS_DIR / "architecture_checksums.json"
    manifest_path.write_text(
        json.dumps({"generated_at": "2026-06-15T00:00:00Z", "files": checksums}, indent=2)
        + "\n"
    )

    for name, expected_sha in checksums.items():
        assert expected_sha == checksums[name], (
            f"Checksum mismatch for {name}"
        )
    assert manifest_path.exists(), "Checksum manifest should have been written"
