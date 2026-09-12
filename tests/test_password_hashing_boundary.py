from __future__ import annotations

import bcrypt

from app.core.security import hash_password, verify_password


def test_new_passwords_use_argon2_and_verify_exactly() -> None:
    password = "StrongPassword123!"

    hashed = hash_password(password)

    assert hashed.startswith("$argon2")
    assert verify_password(password, hashed) is True
    assert verify_password("WrongPassword123!", hashed) is False


def test_new_argon2_hashes_preserve_entropy_beyond_bcrypt_byte_72() -> None:
    shared_prefix = "a" * 72
    password = shared_prefix + "-first-tail"
    different_tail = shared_prefix + "-second-tail"

    hashed = hash_password(password)

    assert verify_password(password, hashed) is True
    assert verify_password(different_tail, hashed) is False


def test_existing_bcrypt_hashes_remain_verifiable() -> None:
    password = "ExistingUserPassword123!"
    legacy_hash = bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt(rounds=12),
    ).decode("ascii")

    assert verify_password(password, legacy_hash) is True
    assert verify_password("WrongPassword123!", legacy_hash) is False


def test_existing_long_bcrypt_password_keeps_historical_verification_semantics() -> None:
    password = ("L" * 72) + "legacy-tail"
    legacy_hash = bcrypt.hashpw(
        password.encode("utf-8")[:72],
        bcrypt.gensalt(rounds=12),
    ).decode("ascii")

    assert verify_password(password, legacy_hash) is True


def test_unknown_or_empty_hash_is_rejected() -> None:
    assert verify_password("password", "") is False
    assert verify_password("password", "not-a-password-hash") is False
