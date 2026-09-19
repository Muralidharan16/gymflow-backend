from __future__ import annotations

import os
import subprocess


DB = os.environ["PAY2B_DATABASE"]
PASSWORDS = {
    "pay2b_finance_login": ("ci-finance", "command", True),
    "pay2b_finance_read_login": ("ci-read", "read", True),
    "pay2b_payment_worker_login": ("ci-payment", "payment_worker", True),
    "pay2b_refund_login": ("ci-refund", "refund", True),
    "pay2b_recon_login": ("ci-recon", "reconciliation", False),
    "pay2b_maintenance_login": ("ci-maint", "maintenance", False),
}
ORG = "11111111-1111-1111-1111-111111111111"


def psql(user: str, password: str, sql: str, *, expect_success: bool = True) -> str:
    env = dict(os.environ)
    env["PGPASSWORD"] = password
    proc = subprocess.run(
        [
            "psql", "-X", "-v", "ON_ERROR_STOP=1",
            "-h", "127.0.0.1", "-U", user, "-d", DB,
            "-Atqc", sql,
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    if expect_success and proc.returncode != 0:
        raise AssertionError(proc.stdout + proc.stderr)
    if not expect_success and proc.returncode == 0:
        raise AssertionError(f"expected failure for {user}: {sql}")
    return (proc.stdout + proc.stderr).strip()


for user, (password, capability, tenant_required) in PASSWORDS.items():
    if tenant_required:
        result = psql(
            user,
            password,
            "BEGIN; "
            f"SELECT pg_catalog.set_config('app.current_org_id','{ORG}',true); "
            f"SELECT app_secure.require_finance_capability('{capability}',true); "
            "ROLLBACK;",
        )
        if ORG not in result:
            raise AssertionError(f"{user} did not receive exact tenant context: {result}")
        psql(
            user,
            password,
            f"SELECT app_secure.require_finance_capability('{capability}',true);",
            expect_success=False,
        )
        psql(
            user,
            password,
            f"SELECT app_secure.require_finance_capability('{capability}',false);",
            expect_success=False,
        )
    else:
        result = psql(
            user,
            password,
            f"SELECT app_secure.require_finance_capability('{capability}',false) IS NULL;",
        )
        if result.splitlines()[-1:] != ["t"]:
            raise AssertionError(f"{user} cross-tenant maintenance guard failed: {result}")

# Cross-capability invocation is forbidden even when app_secure EXECUTE exists.
psql(
    "pay2b_finance_login",
    "ci-finance",
    "BEGIN; "
    f"SELECT pg_catalog.set_config('app.current_org_id','{ORG}',true); "
    "SELECT app_secure.require_finance_capability('refund',true); "
    "ROLLBACK;",
    expect_success=False,
)

# Unknown capability names fail closed.
psql(
    "pay2b_recon_login",
    "ci-recon",
    "SELECT app_secure.require_finance_capability('anything',false);",
    expect_success=False,
)

print("PAY2B_RUNTIME_CAPABILITY_GUARD=PASS")
