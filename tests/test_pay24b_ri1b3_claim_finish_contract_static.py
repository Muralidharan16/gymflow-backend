from __future__ import annotations

import ast
import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

ZZ77 = (
    ROOT
    / "alembic"
    / "versions"
    / "zz77d8e9f0a67_pay24b_ri1b1_claim_finish_expand.py"
)

ZZ87 = (
    ROOT
    / "alembic"
    / "versions"
    / "zz87d8e9f0a68_pay24b_ri1b3_claim_finish_contract.py"
)

ZZ77_FILE_SHA256 = (
    "b926d9dabdb7f0badaf0745347569f64"
    "d2feb5faa323d74616fa05141aefbd39"
)

RI1B1_CLAIM_LITERAL_SHA256 = (
    "122eb82e4c82bd7a105903fa4dcc79e2"
    "b7a39c42f739565d0fed8b2b29445716"
)

RI1B1_FINISH_LITERAL_SHA256 = (
    "90ea6590d1e5d35210c343bb829e8396"
    "482afdbbe122a0eb1a33b2874aeea76c"
)

RI1B3_CLAIM_LITERAL_SHA256 = (
    "5afeda376d3cb5e307f3cd0cd6f68d61"
    "a1c789924575861436308c87a10e5dbc"
)

RI1B3_FINISH_LITERAL_SHA256 = (
    "828e1498df90fe1fc2b22dfef689031e"
    "c2a6016046de11d1c1694d6129415e55"
)


def assignment(
    path: Path,
    name: str,
):
    tree = ast.parse(
        path.read_text()
    )

    for node in tree.body:
        if not isinstance(
            node,
            ast.Assign,
        ):
            continue

        for target in node.targets:
            if (
                isinstance(
                    target,
                    ast.Name,
                )
                and target.id == name
            ):
                return ast.literal_eval(
                    node.value
                )

    raise AssertionError(
        f"missing assignment: {name}"
    )


def test_certified_ri1b1_file_is_exact():
    assert (
        hashlib.sha256(
            ZZ77.read_bytes()
        ).hexdigest()
        == ZZ77_FILE_SHA256
    )


def test_ri1b3_linear_revision():
    assert (
        assignment(
            ZZ87,
            "revision",
        )
        == "zz87d8e9f0a68"
    )

    assert (
        assignment(
            ZZ87,
            "down_revision",
        )
        == "zz77d8e9f0a67"
    )


def test_embedded_dual_sql_is_exact():
    claim = assignment(
        ZZ87,
        "_CLAIM_DUAL_SQL",
    )

    finish = assignment(
        ZZ87,
        "_FINISH_DUAL_SQL",
    )

    assert (
        hashlib.sha256(
            claim.encode()
        ).hexdigest()
        == RI1B1_CLAIM_LITERAL_SHA256
    )

    assert (
        hashlib.sha256(
            finish.encode()
        ).hexdigest()
        == RI1B1_FINISH_LITERAL_SHA256
    )


def test_contracted_sql_hashes_are_exact():
    claim = assignment(
        ZZ87,
        "_CLAIM_CONTRACTED_SQL",
    )

    finish = assignment(
        ZZ87,
        "_FINISH_CONTRACTED_SQL",
    )

    assert (
        hashlib.sha256(
            claim.encode()
        ).hexdigest()
        == RI1B3_CLAIM_LITERAL_SHA256
    )

    assert (
        hashlib.sha256(
            finish.encode()
        ).hexdigest()
        == RI1B3_FINISH_LITERAL_SHA256
    )


def test_claim_guard_is_payment_only():
    claim = assignment(
        ZZ87,
        "_CLAIM_CONTRACTED_SQL",
    )

    assert "'app_runtime'" not in claim

    assert (
        claim.count(
            "'finance_payment_runtime'"
        )
        == 1
    )

    assert (
        "requires finance_payment_runtime"
        in claim
    )


def test_finish_guard_is_payment_only():
    finish = assignment(
        ZZ87,
        "_FINISH_CONTRACTED_SQL",
    )

    assert "'app_runtime'" not in finish

    assert (
        finish.count(
            "'finance_payment_runtime'"
        )
        == 1
    )

    assert (
        "requires finance_payment_runtime"
        in finish
    )


def test_upgrade_removes_app_execute():
    source = ZZ87.read_text()

    upgrade = source.split(
        "def upgrade() -> None:",
        1,
    )[1].split(
        "def downgrade() -> None:",
        1,
    )[0]

    assert (
        "FROM app_runtime"
        in upgrade
    )

    assert (
        "TO finance_payment_runtime"
        in upgrade
    )

    assert (
        "TO app_runtime"
        not in upgrade
    )


def test_downgrade_restores_dual_execute():
    source = ZZ87.read_text()

    downgrade = source.split(
        "def downgrade() -> None:",
        1,
    )[1]

    assert (
        "TO app_runtime"
        in downgrade
    )

    assert (
        "TO finance_payment_runtime"
        in downgrade
    )


def test_bounded_owner_context_is_preserved():
    source = ZZ87.read_text()

    assert (
        "SET LOCAL ROLE app_security_owner"
        in source
    )

    assert (
        "RESET ROLE"
        in source
    )

    assert (
        "session_user=current_user=migration_owner"
        in source
    )


def test_app_runtime_cannot_reach_payment_role():
    source = ZZ87.read_text()

    assert (
        "'app_runtime',"
        in source
    )

    assert (
        "'finance_payment_runtime',"
        in source
    )

    assert (
        "'SET'"
        in source
    )


def test_contract_does_not_modify_reserve_or_reconcile():
    source = ZZ87.read_text()

    assert (
        "reserve_finance_provider_operation("
        not in source
    )

    assert (
        "reconcile_finance_provider_operation("
        not in source
    )


def test_migration_contains_no_provider_io():
    source = ZZ87.read_text().lower()

    for token in (
        "requests.",
        "httpx.",
        "razorpay.",
        "api.razorpay.com",
        "call_provider(",
    ):
        assert token not in source
