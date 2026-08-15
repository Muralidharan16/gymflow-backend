"""One-time P3B legacy registration re-encryption command.

Run only after Alembic has reached i07d8e9f0a29 and before the final P3B
contract revision.  The command deliberately requires an explicit manifest of
verified organization principals rather than inventing or discovering tenant
identity.  It connects through the normal reduced API database login, reads
legacy ciphertext only through the temporary principal-bound capability, and
writes only through the bounded legacy conversion capability.

No registration plaintext, legacy ciphertext, DEK, or KMS ciphertext is logged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

import psycopg
from cryptography.fernet import InvalidToken
from pydantic import ValidationError

from app.core.aws_kms import registration_kms_provider
from app.core.registration_crypto import (
    encrypt_registration_identifier,
    zeroize_key,
)
from app.schemas.organization import RegistrationCreate
from app.utils.encryption import decrypt_data, mask_id_number


@dataclass(frozen=True, slots=True)
class BackfillPrincipal:
    org_id: uuid.UUID
    user_id: uuid.UUID
    principal_type: str
    role: str


@dataclass(frozen=True, slots=True)
class LegacyRegistration:
    id: uuid.UUID
    id_type: str
    encrypted_identifier: str
    masked_identifier: str
    country_code: str


def _load_manifest(path: Path) -> list[BackfillPrincipal]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise RuntimeError("P3B backfill manifest must be a non-empty JSON list")

    principals: list[BackfillPrincipal] = []
    seen_orgs: set[uuid.UUID] = set()
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise RuntimeError(f"manifest entry {index} must be an object")
        expected = {"org_id", "user_id", "principal_type", "role"}
        if set(item) != expected:
            raise RuntimeError(
                f"manifest entry {index} must contain exactly {sorted(expected)}"
            )
        try:
            org_id = uuid.UUID(str(item["org_id"]))
            user_id = uuid.UUID(str(item["user_id"]))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"manifest entry {index} has an invalid UUID") from exc
        principal_type = str(item["principal_type"]).strip()
        role = str(item["role"]).strip()
        if principal_type not in {"owner", "organization_user"}:
            raise RuntimeError(f"manifest entry {index} has invalid principal_type")
        if role not in {"owner", "admin"}:
            raise RuntimeError(f"manifest entry {index} has invalid role")
        if org_id in seen_orgs:
            raise RuntimeError(f"manifest contains duplicate organization {org_id}")
        seen_orgs.add(org_id)
        principals.append(
            BackfillPrincipal(
                org_id=org_id,
                user_id=user_id,
                principal_type=principal_type,
                role=role,
            )
        )
    return principals


def _set_context(cursor, principal: BackfillPrincipal) -> None:
    for name, value in (
        ("app.current_org_id", str(principal.org_id)),
        ("app.current_user_id", str(principal.user_id)),
        ("app.current_principal_type", principal.principal_type),
        ("app.current_role", principal.role),
        ("app.current_gym_id", ""),
    ):
        cursor.execute(
            "SELECT pg_catalog.set_config(%s, %s, true)",
            (name, value),
        )


def _assert_reduced_api_login(cursor) -> None:
    cursor.execute(
        """
        SELECT session_user::text,
               role_data.rolsuper,
               role_data.rolcreatedb,
               role_data.rolcreaterole,
               role_data.rolreplication,
               role_data.rolbypassrls,
               pg_catalog.pg_has_role(session_user, 'app_runtime', 'MEMBER')
        FROM pg_catalog.pg_roles AS role_data
        WHERE role_data.rolname = session_user
        """
    )
    row = cursor.fetchone()
    if row is None or any(bool(value) for value in row[1:6]) or not bool(row[6]):
        raise RuntimeError("P3B backfill requires a reduced login that is a member of app_runtime")
    if row[0] in {"migration_owner", "app_security_owner"}:
        raise RuntimeError("P3B backfill must not run as a database owner/security role")


def _legacy_rows(cursor) -> list[LegacyRegistration]:
    cursor.execute("SELECT * FROM app_secure.current_legacy_registration_backfill_rows()")
    return [
        LegacyRegistration(
            id=uuid.UUID(str(row[0])),
            id_type=str(row[1]),
            encrypted_identifier=str(row[2]),
            masked_identifier=str(row[3]),
            country_code=str(row[4]),
        )
        for row in cursor.fetchall()
    ]


def _current_key(cursor):
    cursor.execute("SELECT * FROM app_secure.current_registration_dek()")
    return cursor.fetchone()


def _install_key(cursor, ciphertext: bytes, wrapping_key_id: str):
    cursor.execute(
        "SELECT * FROM app_secure.install_registration_dek(%s, %s)",
        (ciphertext, wrapping_key_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("registration DEK installation returned no winner")
    return row


def _convert_row(cursor, row: LegacyRegistration, payload: bytes, key_version: int) -> None:
    cursor.execute(
        """
        SELECT *
        FROM app_secure.convert_legacy_organization_registration_envelope(
            %s, %s, %s
        )
        """,
        (row.id, payload, key_version),
    )
    converted = cursor.fetchone()
    if converted is None or uuid.UUID(str(converted[0])) != row.id:
        raise RuntimeError("legacy registration conversion returned an invalid target")
    if str(converted[2]) != row.masked_identifier:
        raise RuntimeError("legacy registration conversion changed masked metadata")


async def _backfill_principal(dsn: str, principal: BackfillPrincipal) -> int:
    with psycopg.connect(dsn) as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                _assert_reduced_api_login(cursor)
                _set_context(cursor, principal)
                legacy_rows = _legacy_rows(cursor)
                if not legacy_rows:
                    return 0

                kms = registration_kms_provider(str(principal.org_id))
                key_row = _current_key(cursor)
                if key_row is None:
                    candidate = await kms.generate_encrypted_data_key()
                    key_row = _install_key(cursor, candidate.ciphertext, candidate.key_id)

                key_version = int(key_row[0])
                wrapped_dek = bytes(key_row[1])
                wrapping_key_id = str(key_row[2])
                raw_dek = bytearray(
                    await kms.decrypt_dek(wrapped_dek, wrapping_key_id)
                )
                try:
                    converted_count = 0
                    for legacy in legacy_rows:
                        try:
                            identifier = decrypt_data(legacy.encrypted_identifier)
                        except InvalidToken as exc:
                            raise RuntimeError(
                                f"legacy registration {legacy.id} cannot be decrypted"
                            ) from exc
                        if mask_id_number(identifier) != legacy.masked_identifier:
                            raise RuntimeError(
                                f"legacy registration {legacy.id} mask does not match plaintext"
                            )
                        try:
                            validated = RegistrationCreate(
                                id_type=legacy.id_type,
                                id_number=identifier,
                                country_code=legacy.country_code,
                            )
                        except ValidationError as exc:
                            raise RuntimeError(
                                f"legacy registration {legacy.id} fails current validation"
                            ) from exc
                        if (
                            validated.id_type.strip().upper() != legacy.id_type
                            or validated.country_code.strip().upper() != legacy.country_code
                        ):
                            raise RuntimeError(
                                f"legacy registration {legacy.id} metadata is not canonical"
                            )
                        envelope = encrypt_registration_identifier(
                            validated.id_number,
                            key=raw_dek,
                            tenant_id=principal.org_id,
                            registration_id=legacy.id,
                            key_version=key_version,
                        )
                        _convert_row(cursor, legacy, envelope.payload, key_version)
                        converted_count += 1
                    return converted_count
                finally:
                    zeroize_key(raw_dek)


async def _run(manifest_path: Path) -> int:
    dsn = os.environ.get("P3B_API_DSN", "").strip()
    if not dsn:
        raise RuntimeError("P3B_API_DSN is required")
    principals = _load_manifest(manifest_path)
    total = 0
    for principal in principals:
        converted = await _backfill_principal(dsn, principal)
        total += converted
        print(
            f"P3B backfill organization {principal.org_id}: converted={converted}"
        )
    print(f"P3B legacy registration backfill complete: converted={total}")
    return total


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert P3B legacy registration ciphertext to KMS envelopes"
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="JSON file containing explicit org/user principal bindings",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.manifest))


if __name__ == "__main__":
    main()
