from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "alembic/versions/zt07d8e9f0a54_pay10_refund_provider_authority.py"
)
MODEL = ROOT / "app/finance_core/models/foundation.py"
ARCHITECTURE = (
    ROOT
    / "docs/architecture/PAY10_REFUND_CREDIT_NOTE_PROVIDER_EXECUTION.md"
)
MACHINE = ROOT / "docs/architecture/pay10_refund_provider_execution_v1.json"
PROVIDER_BOUNDARY = ROOT / "app/finance_core/domain/provider_boundary.py"
RAZORPAY_DOMAIN = ROOT / "app/finance_core/domain/razorpay_sandbox.py"
RAZORPAY_SERVICE = ROOT / "app/finance_core/services/razorpay_sandbox.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_pay10_a_is_exact_pay9_successor():
    source = _text(MIGRATION)
    assert 'revision = "zt07d8e9f0a54"' in source
    assert 'down_revision = "zs07d8e9f0a53"' in source
    assert "SET LOCAL lock_timeout='3s'" in source
    assert "SET LOCAL statement_timeout='30s'" in source
    assert "PAY-10 migration requires session_user=current_user=migration_owner" in source


def test_pay10_a_adds_closed_provider_evidence_and_credit_note_provenance():
    source = _text(MIGRATION)
    for relation in (
        "finance.credit_note_series",
        "finance.refund_credit_note_links",
        "finance.refund_provider_evidence",
    ):
        assert f"CREATE TABLE {relation}" in source

    assert "_NEW_TABLES = (" in source
    assert 'f"ALTER TABLE finance.{table_name} ENABLE ROW LEVEL SECURITY"' in source
    assert 'f"ALTER TABLE finance.{table_name} FORCE ROW LEVEL SECURITY"' in source
    assert 'f"REVOKE ALL ON TABLE finance.{table_name} FROM PUBLIC"' in source

    assert "uq_pay10_refund_credit_note_single_refund" in source
    assert "fk_pay10_refund_credit_link_refund_org" in source
    assert "fk_pay10_refund_credit_link_credit_org" in source
    assert "uq_pay10_refund_provider_event" in source
    assert "uq_pay10_refund_command_provider_ref" in source
    assert "request_sha256 CHAR(64)" in source
    assert "provider_accepted_at TIMESTAMPTZ" in source
    assert "completed_at TIMESTAMPTZ" in source


def test_pay10_provider_evidence_is_immutable_and_redacted_by_shape():
    source = _text(MIGRATION)
    assert "pay10_immutable_refund_provider_evidence" in source
    assert "app_secure.pay2_reject_finance_immutable_history_mutation()" in source
    assert "BEFORE UPDATE OR DELETE" in source

    forbidden_columns = (
        "raw_body",
        "raw_payload",
        "authorization_header",
        "api_key",
        "api_secret",
        "card_number",
        "cvv",
        "access_token",
    )
    table_body = source.split(
        "CREATE TABLE finance.refund_provider_evidence", 1
    )[1].split(
        "CREATE UNIQUE INDEX uq_pay10_refund_provider_event", 1
    )[0].lower()
    for name in forbidden_columns:
        assert name not in table_body

    assert "request_sha256" in table_body
    assert "evidence_sha256" in table_body


def test_pay10_runtime_roles_remain_table_blind():
    source = _text(MIGRATION)
    for role in (
        "app_runtime",
        "worker_runtime",
        "finance_runtime",
        "finance_payment_runtime",
        "finance_refund_runtime",
        "finance_reconciliation_runtime",
        "finance_read_runtime",
        "finance_maintenance_runtime",
    ):
        for verb in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            for table_name in (
                "credit_note_series",
                "refund_credit_note_links",
                "refund_provider_evidence",
            ):
                forbidden = (
                    f"GRANT {verb} ON TABLE finance.{table_name} TO {role}"
                )
                assert forbidden not in source

    assert "GRANT SELECT, INSERT, UPDATE\n        ON TABLE finance.credit_note_series\n        TO app_security_owner" in source
    assert "GRANT SELECT, INSERT\n        ON TABLE finance.refund_credit_note_links\n        TO app_security_owner" in source
    assert "GRANT SELECT, INSERT\n        ON TABLE finance.refund_provider_evidence\n        TO app_security_owner" in source


def test_pay10_model_metadata_matches_new_schema_and_tenant_fk_prerequisites():
    source = _text(MODEL)
    for model in (
        "class FinanceCreditNoteSeries(Base):",
        "class FinanceRefundCreditNoteLink(Base):",
        "class FinanceRefundProviderEvidence(Base):",
    ):
        assert model in source

    assert 'name="uq_pay10_refunds_id_org"' in source
    assert 'name="uq_pay10_credit_notes_id_org"' in source
    assert 'name="fk_pay10_refund_credit_link_refund_org"' in source
    assert 'name="fk_pay10_refund_credit_link_credit_org"' in source
    assert '"uq_pay10_refund_command_provider_ref",' in source
    assert 'name="chk_pay10_refund_command_request_hash"' in source


def test_pay10_credit_note_numbering_and_refund_cash_accounting_are_separate_authorities():
    architecture = _text(ARCHITECTURE)
    machine = json.loads(_text(MACHINE))

    assert "Credit notes reverse revenue/tax and" in architecture
    assert "debits AR and credits" in architecture
    assert "`PAYMENT_CLEARING`" in architecture
    assert "without reversing revenue a" in architecture

    assert machine["refund_cash_ledger"] == [
        "debit:AR",
        "credit:PAYMENT_CLEARING",
    ]
    assert machine["revenue_tax_reversal_authority"] == "credit_note_only"
    assert "issued_credit_note_backing" in machine["terminal_refund_requires"]


def test_pay10_a_downgrade_fails_closed_when_durable_evidence_exists():
    source = _text(MIGRATION)
    assert "PAY-10 refuses populated downgrade" in source
    assert "PAY-10 refuses downgrade with refund execution attempt evidence" in source
    assert "DROP TABLE finance.refund_provider_evidence" in source
    assert "DROP TABLE finance.refund_credit_note_links" in source
    assert "DROP TABLE finance.credit_note_series" in source
    assert "CASCADE" not in source


def test_pay10_machine_contract_keeps_live_money_and_release_disabled():
    contract = json.loads(_text(MACHINE))
    assert contract["phase"] == "PAY-10"
    assert contract["inherited_pay9_sha"] == (
        "581c3d83a1e78ba59213c90309166913fedf9c3e"
    )
    assert contract["predecessor_revision"] == "zs07d8e9f0a53"
    assert contract["revision"] == "zt07d8e9f0a54"
    assert contract["live_provider"] is False
    assert contract["live_money_movement"] is False
    assert contract["merge_authorized"] is False
    assert contract["release_authorized"] is False
    assert contract["deployment_authorized"] is False


def test_pay10_a_contains_no_live_provider_secret_or_money_execution():
    combined = "\n".join(
        _text(path).lower()
        for path in (MIGRATION, MODEL, ARCHITECTURE, MACHINE)
    )
    assert "rzp_live_" not in combined
    assert "razorpay_key_secret" not in combined
    assert "authorization: basic" not in combined
    assert "httpx.post(" not in combined
    assert "requests.post(" not in combined
    assert "aiohttp" not in combined


def test_pay10_b_provider_boundary_is_server_authoritative_and_provider_neutral():
    source = _text(PROVIDER_BOUNDARY)
    assert "class ProviderRefundRequest:" in source
    assert "command_id: uuid.UUID" in source
    assert "refund_id: uuid.UUID" in source
    assert "payment_id: uuid.UUID" in source
    assert "provider_payment_ref: str" in source
    assert "amount: Decimal" in source
    assert "currency_code: str" in source
    assert 'ProviderRefundStatus = Literal["pending", "processed", "failed"]' in source
    assert "class RefundProvider(Protocol):" in source
    assert "async def submit_refund(" in source
    assert "async def fetch_refund(" in source


def test_pay10_b_razorpay_adapter_has_submit_fetch_and_stable_receipt_contract():
    domain = _text(RAZORPAY_DOMAIN)
    service = _text(RAZORPAY_SERVICE)
    contract = json.loads(_text(MACHINE))

    assert "class RazorpayRefundRequest:" in domain
    assert "class RazorpayRefundResponse:" in domain
    assert "def map_razorpay_refund_response(" in domain
    assert '"pending", "processed", "failed"' in domain

    assert "class RazorpayTestModeRefundsClient:" in service
    assert 'f"{request.provider_payment_id}/refund"' in service
    assert 'f"{request.provider_payment_id}/refunds/{provider_refund_id}"' in service
    assert 'receipt_seed = f"{request.command_id}:{request.refund_id}"' in service
    assert '"rf_"' in service
    assert "hashlib.sha256" in service

    adapter = contract["provider_adapter"]
    assert adapter["provider_code"] == "razorpay_sandbox"
    assert adapter["environments"] == ["sandbox", "test"]
    assert adapter["normalized_states"] == ["pending", "processed", "failed"]
    assert adapter["duplicate_receipt_http_400"] == "reconciliation_required"
    assert adapter["automatic_retry"] == "known non-acceptance only"
    assert adapter["database_mutation"] is False


def test_pay10_b_unknown_refund_outcomes_reconcile_instead_of_blind_retry():
    domain = _text(RAZORPAY_DOMAIN)
    service = _text(RAZORPAY_SERVICE)

    assert 'operation == "submit_refund" and provider_status_code == 400' in domain
    for code in (
        "RAZORPAY_REFUND_RESPONSE_INVALID",
        "RAZORPAY_REFUND_ID_INVALID",
        "RAZORPAY_REFUND_PAYMENT_MISMATCH",
        "RAZORPAY_REFUND_AMOUNT_MISMATCH",
        "RAZORPAY_REFUND_CURRENCY_MISMATCH",
        "RAZORPAY_REFUND_RECEIPT_MISMATCH",
        "RAZORPAY_REFUND_STATUS_INVALID",
    ):
        assert code in domain

    assert '"RAZORPAY_CONNECT_FAILED"' in domain
    assert 'return "retryable"' in domain
    assert '"RAZORPAY_TIMEOUT"' in domain
    assert 'return "unknown"' in domain

    assert "await self._transport.get_json(" in service
    assert "await self._transport.post_json(" in service


def test_pay10_b_adapter_does_not_gain_finance_database_mutation_authority():
    service = _text(RAZORPAY_SERVICE).lower()
    for forbidden in (
        "asyncsession",
        "session.execute",
        "insert(",
        "update(",
        "delete(",
        "finance.refunds",
        "finance.refund_execution_commands",
        "finance.refund_provider_evidence",
        "finance.payments",
        "finance.ledger",
    ):
        assert forbidden not in service

    assert "rzp_live_" not in service
    assert "requests.post(" not in service
    assert "httpx" not in service
    assert "aiohttp" not in service
