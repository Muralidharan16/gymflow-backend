from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings
from app.platform_billing.domain.hashing import CanonicalSerializer
from app.platform_billing.domain.reconciliation import (
    PROVIDER_PENDING,
    PROVIDER_TERMINAL_FAILED,
    PROVIDER_TERMINAL_SUCCEEDED,
    ProviderOperationEvidence,
    ReconciliationPage,
    ReconciliationProviderFailure,
    ReconciliationRunRequest,
    compute_evidence_hash,
)


FAKE_CHECKOUT_EVIDENCE_SCHEMA_VERSION = "fake-checkout-provider-evidence-v1"
FAKE_PROVIDER_EVIDENCE_KEY_PURPOSE = "platform-billing-fake-provider-evidence-v1"
TERMINAL_OUTCOMES = frozenset({"succeeded", "failed"})
SUPPORTED_OUTCOMES = frozenset({"pending", "succeeded", "failed"})


class FakeCheckoutEvidenceError(Exception):
    pass


class FakeCheckoutEvidenceConflict(FakeCheckoutEvidenceError):
    pass


class FakeCheckoutEvidenceCorrupt(FakeCheckoutEvidenceError):
    pass


class FakeCheckoutEvidenceStorageFailure(FakeCheckoutEvidenceError):
    pass


@dataclass(frozen=True)
class FakeCheckoutProviderEvidence:
    schema_version: str
    provider_code: str
    organization_id: uuid.UUID
    confirm_checkout_operation_id: uuid.UUID
    checkout_operation_id: uuid.UUID
    external_operation_ref: str
    checkout_session_reference: str
    provider_customer_ref: str
    provider_outcome: str
    provider_observed_at: datetime
    canonical_evidence_hash: str
    provider_event_id: str | None = None
    raw_event_sha256: str | None = None
    encrypted_raw_event_ref: str | None = None
    raw_event: bytes | None = None
    signature_header: str | None = None
    signature_timestamp: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int = 1

    @property
    def provider_status(self) -> str:
        if self.provider_outcome == "succeeded":
            return PROVIDER_TERMINAL_SUCCEEDED
        if self.provider_outcome == "failed":
            return PROVIDER_TERMINAL_FAILED
        return PROVIDER_PENDING


class FakeCheckoutEvidenceStore(Protocol):
    async def record(self, evidence: FakeCheckoutProviderEvidence) -> FakeCheckoutProviderEvidence:
        """Record provider-side fake checkout evidence or return existing idempotent evidence."""

    async def get(
        self,
        *,
        provider_code: str,
        organization_id: uuid.UUID,
        external_operation_ref: str,
    ) -> FakeCheckoutProviderEvidence | None:
        """Load trusted provider evidence by tenant-scoped external operation reference."""


@dataclass(frozen=True)
class LocalEncryptedFakeCheckoutEvidenceStore:
    root_dir: Path
    secret: str | None = None

    async def record(self, evidence: FakeCheckoutProviderEvidence) -> FakeCheckoutProviderEvidence:
        if evidence.provider_outcome not in SUPPORTED_OUTCOMES:
            raise FakeCheckoutEvidenceStorageFailure("Unsupported fake checkout evidence outcome")
        self._validate_root()
        path = self._path_for(evidence.provider_code, evidence.organization_id, evidence.external_operation_ref)
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise FakeCheckoutEvidenceStorageFailure("Fake provider evidence directory is not usable") from exc
        _chmod_private_dir(path.parent)
        lock_path = path.with_suffix(path.suffix + ".lock")
        with _file_lock(lock_path):
            current = await self.get(
                provider_code=evidence.provider_code,
                organization_id=evidence.organization_id,
                external_operation_ref=evidence.external_operation_ref,
            )
            merged = _merge_evidence(current, evidence)
            if current is not None and merged.canonical_evidence_hash == current.canonical_evidence_hash:
                return current
            self._write_atomic(path, merged)
            return merged

    async def get(
        self,
        *,
        provider_code: str,
        organization_id: uuid.UUID,
        external_operation_ref: str,
    ) -> FakeCheckoutProviderEvidence | None:
        self._validate_root()
        path = self._path_for(provider_code, organization_id, external_operation_ref)
        if not path.exists():
            return None
        return self._read(path, provider_code=provider_code, organization_id=organization_id, external_operation_ref=external_operation_ref)

    async def list_for_request(self, request: ReconciliationRunRequest) -> tuple[FakeCheckoutProviderEvidence, ...]:
        self._validate_root()
        refs = request.scope.get("external_operation_refs")
        if request.provider_code != "fake" or request.organization_id is None or not isinstance(refs, list):
            return ()
        found: list[FakeCheckoutProviderEvidence] = []
        for external_ref in refs:
            if not isinstance(external_ref, str):
                continue
            evidence = await self.get(
                provider_code=request.provider_code,
                organization_id=request.organization_id,
                external_operation_ref=external_ref,
            )
            if evidence is not None:
                found.append(evidence)
        return tuple(found)

    def _validate_root(self) -> None:
        root = self.root_dir.expanduser()
        repo = Path(__file__).resolve().parents[3]
        try:
            root.resolve().relative_to(repo)
        except ValueError:
            pass
        else:
            raise FakeCheckoutEvidenceStorageFailure("Fake provider evidence directory must be outside the source repository")
        if not str(root):
            raise FakeCheckoutEvidenceStorageFailure("Fake provider evidence directory is not configured")

    def _path_for(self, provider_code: str, organization_id: uuid.UUID, external_operation_ref: str) -> Path:
        digest = _identity_digest(provider_code=provider_code, organization_id=organization_id, external_operation_ref=external_operation_ref)
        return self.root_dir / provider_code / digest[:2] / f"{digest}.evidence"

    def _write_atomic(self, path: Path, evidence: FakeCheckoutProviderEvidence) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        plaintext = json.dumps(_evidence_to_payload(evidence), sort_keys=True, separators=(",", ":")).encode("utf-8")
        nonce = os.urandom(12)
        associated_data = _associated_data(evidence.provider_code, evidence.organization_id, evidence.external_operation_ref)
        ciphertext = AESGCM(_encryption_key(self.secret)).encrypt(nonce, plaintext, associated_data)
        envelope = {
            "schema_version": FAKE_CHECKOUT_EVIDENCE_SCHEMA_VERSION,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }
        encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            os.chmod(path, 0o600)
            _fsync_dir(path.parent)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise FakeCheckoutEvidenceStorageFailure("Fake checkout provider evidence could not be stored") from exc

    def _read(
        self,
        path: Path,
        *,
        provider_code: str,
        organization_id: uuid.UUID,
        external_operation_ref: str,
    ) -> FakeCheckoutProviderEvidence:
        envelope = _load_envelope(path)
        payload = _decrypt_envelope(
            envelope,
            key=_encryption_key(self.secret),
            associated_data_candidates=(
                _associated_data(provider_code, organization_id, external_operation_ref),
            ),
        )
        evidence = _payload_to_evidence(payload)
        if (
            evidence.provider_code != provider_code
            or evidence.organization_id != organization_id
            or evidence.external_operation_ref != external_operation_ref
        ):
            raise FakeCheckoutEvidenceCorrupt("Fake provider evidence identity mismatch")
        _assert_hash(evidence)
        return evidence


@dataclass
class InMemoryFakeCheckoutEvidenceStore:
    records: dict[tuple[str, uuid.UUID, str], FakeCheckoutProviderEvidence] | None = None

    def __post_init__(self) -> None:
        if self.records is None:
            self.records = {}

    async def record(self, evidence: FakeCheckoutProviderEvidence) -> FakeCheckoutProviderEvidence:
        key = (evidence.provider_code, evidence.organization_id, evidence.external_operation_ref)
        assert self.records is not None
        current = self.records.get(key)
        merged = _merge_evidence(current, evidence)
        self.records[key] = merged
        return merged

    async def get(
        self,
        *,
        provider_code: str,
        organization_id: uuid.UUID,
        external_operation_ref: str,
    ) -> FakeCheckoutProviderEvidence | None:
        assert self.records is not None
        return self.records.get((provider_code, organization_id, external_operation_ref))

    async def list_for_request(self, request: ReconciliationRunRequest) -> tuple[FakeCheckoutProviderEvidence, ...]:
        assert self.records is not None
        return tuple(
            evidence
            for evidence in self.records.values()
            if evidence.provider_code == request.provider_code
            and (request.organization_id is None or evidence.organization_id == request.organization_id)
        )


class LocalFakeCheckoutProviderEvidenceReader:
    def __init__(self, store: LocalEncryptedFakeCheckoutEvidenceStore | InMemoryFakeCheckoutEvidenceStore):
        self._store = store

    async def list_operation_evidence(self, request: ReconciliationRunRequest) -> ReconciliationPage:
        evidence = tuple(_provider_evidence(record) for record in await self._store.list_for_request(request))
        return ReconciliationPage(evidence=evidence, next_watermark=dict(request.watermark))

    async def fetch_operation_evidence(self, evidence_ref: str) -> ProviderOperationEvidence:
        parts = evidence_ref.split(":")
        if len(parts) != 5 or parts[:2] != ["fake-provider-evidence", "v1"]:
            raise ReconciliationProviderFailure("provider_evidence_missing")
        try:
            organization_id = uuid.UUID(parts[3])
        except ValueError as exc:
            raise ReconciliationProviderFailure("provider_evidence_missing") from exc
        record = await self._store.get(
            provider_code=parts[2],
            organization_id=organization_id,
            external_operation_ref=parts[4],
        )
        if record is None:
            raise ReconciliationProviderFailure("provider_evidence_missing")
        return _provider_evidence(record)


def build_pending_evidence(
    *,
    organization_id: uuid.UUID,
    confirm_checkout_operation_id: uuid.UUID,
    checkout_operation_id: uuid.UUID,
    external_operation_ref: str,
    checkout_session_reference: str,
    provider_customer_ref: str,
    observed_at: datetime | None = None,
) -> FakeCheckoutProviderEvidence:
    now = observed_at or datetime.now(timezone.utc)
    return _with_hash(
        FakeCheckoutProviderEvidence(
            schema_version=FAKE_CHECKOUT_EVIDENCE_SCHEMA_VERSION,
            provider_code="fake",
            organization_id=organization_id,
            confirm_checkout_operation_id=confirm_checkout_operation_id,
            checkout_operation_id=checkout_operation_id,
            external_operation_ref=external_operation_ref,
            checkout_session_reference=checkout_session_reference,
            provider_customer_ref=provider_customer_ref,
            provider_outcome="pending",
            provider_observed_at=now,
            canonical_evidence_hash="",
            created_at=now,
            updated_at=now,
        )
    )


def build_terminal_evidence(
    *,
    organization_id: uuid.UUID,
    confirm_checkout_operation_id: uuid.UUID,
    checkout_operation_id: uuid.UUID,
    external_operation_ref: str,
    checkout_session_reference: str,
    provider_customer_ref: str,
    provider_outcome: str,
    provider_event_id: str,
    raw_event: bytes,
    signature_header: str,
    signature_timestamp: int,
    observed_at: datetime | None = None,
) -> FakeCheckoutProviderEvidence:
    if provider_outcome not in TERMINAL_OUTCOMES:
        raise FakeCheckoutEvidenceStorageFailure("Terminal evidence requires a terminal outcome")
    now = observed_at or datetime.fromtimestamp(signature_timestamp, tz=timezone.utc)
    return _with_hash(
        FakeCheckoutProviderEvidence(
            schema_version=FAKE_CHECKOUT_EVIDENCE_SCHEMA_VERSION,
            provider_code="fake",
            organization_id=organization_id,
            confirm_checkout_operation_id=confirm_checkout_operation_id,
            checkout_operation_id=checkout_operation_id,
            external_operation_ref=external_operation_ref,
            checkout_session_reference=checkout_session_reference,
            provider_customer_ref=provider_customer_ref,
            provider_outcome=provider_outcome,
            provider_observed_at=now,
            canonical_evidence_hash="",
            provider_event_id=provider_event_id,
            raw_event_sha256=hashlib.sha256(raw_event).hexdigest(),
            encrypted_raw_event_ref=f"fake-provider-evidence:v1:{provider_event_id}",
            raw_event=raw_event,
            signature_header=signature_header,
            signature_timestamp=signature_timestamp,
            created_at=now,
            updated_at=now,
        )
    )


def default_fake_checkout_evidence_store() -> LocalEncryptedFakeCheckoutEvidenceStore:
    return LocalEncryptedFakeCheckoutEvidenceStore(Path(settings.PLATFORM_BILLING_FAKE_PROVIDER_EVIDENCE_DIR))


def default_fake_checkout_evidence_reader() -> LocalFakeCheckoutProviderEvidenceReader:
    return LocalFakeCheckoutProviderEvidenceReader(default_fake_checkout_evidence_store())


def _provider_evidence(record: FakeCheckoutProviderEvidence) -> ProviderOperationEvidence:
    safe = {
        "checkout_operation_id": str(record.checkout_operation_id),
        "checkout_session_reference": record.checkout_session_reference,
        "external_operation_ref": record.external_operation_ref,
        "provider_code": record.provider_code,
        "provider_customer_reference": record.provider_customer_ref,
        "provider_event_id": record.provider_event_id,
        "provider_observed_at": record.provider_observed_at,
        "provider_status": record.provider_status,
        "raw_event_sha256": record.raw_event_sha256,
    }
    return ProviderOperationEvidence(
        provider_code=record.provider_code,
        external_operation_ref=record.external_operation_ref,
        provider_status=record.provider_status,
        observed_at=record.provider_observed_at,
        evidence_ref=_evidence_ref(record),
        evidence_sha256=record.canonical_evidence_hash,
        safe_evidence=safe,
    )


def _merge_evidence(
    current: FakeCheckoutProviderEvidence | None,
    incoming: FakeCheckoutProviderEvidence,
) -> FakeCheckoutProviderEvidence:
    if current is None:
        return incoming
    if current.canonical_evidence_hash == incoming.canonical_evidence_hash:
        return current
    if current.provider_outcome == "pending" and incoming.provider_outcome == "pending":
        return current
    if current.provider_outcome == "pending" and incoming.provider_outcome in TERMINAL_OUTCOMES:
        return replace(incoming, created_at=current.created_at or incoming.created_at, version=current.version + 1)
    if current.provider_outcome in TERMINAL_OUTCOMES and incoming.provider_outcome == current.provider_outcome:
        if current.provider_event_id == incoming.provider_event_id and current.raw_event_sha256 == incoming.raw_event_sha256:
            return current
        raise FakeCheckoutEvidenceConflict("Same fake provider event identity has conflicting evidence")
    raise FakeCheckoutEvidenceConflict("Fake checkout terminal provider evidence is immutable")


def _with_hash(evidence: FakeCheckoutProviderEvidence) -> FakeCheckoutProviderEvidence:
    return replace(evidence, canonical_evidence_hash=_canonical_hash(evidence))


def _canonical_hash(evidence: FakeCheckoutProviderEvidence) -> str:
    payload = _safe_payload(evidence)
    return compute_evidence_hash(payload)


def _safe_payload(evidence: FakeCheckoutProviderEvidence) -> dict:
    return {
        "checkout_operation_id": str(evidence.checkout_operation_id),
        "checkout_session_reference": evidence.checkout_session_reference,
        "confirm_checkout_operation_id": str(evidence.confirm_checkout_operation_id),
        "external_operation_ref": evidence.external_operation_ref,
        "provider_code": evidence.provider_code,
        "provider_customer_ref": evidence.provider_customer_ref,
        "provider_event_id": evidence.provider_event_id,
        "provider_observed_at": evidence.provider_observed_at.isoformat(),
        "provider_outcome": evidence.provider_outcome,
        "raw_event_sha256": evidence.raw_event_sha256,
        "schema_version": evidence.schema_version,
        "signature_header": evidence.signature_header,
        "signature_timestamp": evidence.signature_timestamp,
        "organization_id": str(evidence.organization_id),
    }


def _evidence_to_payload(evidence: FakeCheckoutProviderEvidence) -> dict:
    payload = _safe_payload(evidence)
    payload.update(
        {
            "canonical_evidence_hash": evidence.canonical_evidence_hash,
            "created_at": (evidence.created_at or evidence.provider_observed_at).isoformat(),
            "encrypted_raw_event_ref": evidence.encrypted_raw_event_ref,
            "raw_event_b64": base64.b64encode(evidence.raw_event or b"").decode("ascii") if evidence.raw_event is not None else None,
            "updated_at": (evidence.updated_at or evidence.provider_observed_at).isoformat(),
            "version": evidence.version,
        }
    )
    return payload


def _payload_to_evidence(payload: dict) -> FakeCheckoutProviderEvidence:
    raw = payload.get("raw_event_b64")
    try:
        raw_event = base64.b64decode(raw) if raw is not None else None
        return FakeCheckoutProviderEvidence(
            schema_version=payload["schema_version"],
            provider_code=payload["provider_code"],
            organization_id=uuid.UUID(payload["organization_id"]),
            confirm_checkout_operation_id=uuid.UUID(payload["confirm_checkout_operation_id"]),
            checkout_operation_id=uuid.UUID(payload["checkout_operation_id"]),
            external_operation_ref=payload["external_operation_ref"],
            checkout_session_reference=payload["checkout_session_reference"],
            provider_customer_ref=payload["provider_customer_ref"],
            provider_outcome=payload["provider_outcome"],
            provider_observed_at=datetime.fromisoformat(payload["provider_observed_at"]),
            canonical_evidence_hash=payload["canonical_evidence_hash"],
            provider_event_id=payload.get("provider_event_id"),
            raw_event_sha256=payload.get("raw_event_sha256"),
            encrypted_raw_event_ref=payload.get("encrypted_raw_event_ref"),
            raw_event=raw_event,
            signature_header=payload.get("signature_header"),
            signature_timestamp=payload.get("signature_timestamp"),
            created_at=datetime.fromisoformat(payload["created_at"]) if payload.get("created_at") else None,
            updated_at=datetime.fromisoformat(payload["updated_at"]) if payload.get("updated_at") else None,
            version=int(payload.get("version") or 1),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FakeCheckoutEvidenceCorrupt("Fake provider evidence payload is malformed") from exc


def _assert_hash(evidence: FakeCheckoutProviderEvidence) -> None:
    if evidence.schema_version != FAKE_CHECKOUT_EVIDENCE_SCHEMA_VERSION:
        raise FakeCheckoutEvidenceCorrupt("Unsupported fake provider evidence schema")
    if evidence.provider_outcome not in SUPPORTED_OUTCOMES:
        raise FakeCheckoutEvidenceCorrupt("Unsupported fake provider evidence outcome")
    if _canonical_hash(evidence) != evidence.canonical_evidence_hash:
        raise FakeCheckoutEvidenceCorrupt("Fake provider evidence hash mismatch")
    if evidence.raw_event is not None and hashlib.sha256(evidence.raw_event).hexdigest() != evidence.raw_event_sha256:
        raise FakeCheckoutEvidenceCorrupt("Fake provider raw event hash mismatch")


def _load_envelope(path: Path) -> dict:
    try:
        envelope = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FakeCheckoutEvidenceCorrupt("Fake provider evidence file is unreadable") from exc
    if envelope.get("schema_version") != FAKE_CHECKOUT_EVIDENCE_SCHEMA_VERSION:
        raise FakeCheckoutEvidenceCorrupt("Unsupported fake provider evidence envelope")
    return envelope


def _decrypt_envelope(
    envelope: dict,
    *,
    key: bytes,
    associated_data_candidates: tuple[bytes, ...] | None,
) -> dict:
    try:
        nonce = base64.b64decode(envelope["nonce"])
        ciphertext = base64.b64decode(envelope["ciphertext"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FakeCheckoutEvidenceCorrupt("Fake provider evidence envelope is malformed") from exc
    candidates = associated_data_candidates or (None,)
    for associated_data in candidates:
        try:
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, associated_data)
            return json.loads(plaintext.decode("utf-8"))
        except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError):
            continue
    raise FakeCheckoutEvidenceCorrupt("Fake provider evidence integrity check failed")


def _associated_data(provider_code: str, organization_id: uuid.UUID, external_operation_ref: str) -> bytes:
    return CanonicalSerializer.serialize(
        {
            "external_operation_ref": external_operation_ref,
            "organization_id": organization_id,
            "provider_code": provider_code,
            "schema_version": FAKE_CHECKOUT_EVIDENCE_SCHEMA_VERSION,
        }
    ).encode("utf-8")


def _identity_digest(*, provider_code: str, organization_id: uuid.UUID, external_operation_ref: str) -> str:
    return hashlib.sha256(_associated_data(provider_code, organization_id, external_operation_ref)).hexdigest()


def _evidence_ref(record: FakeCheckoutProviderEvidence) -> str:
    return f"fake-provider-evidence:v1:{record.provider_code}:{record.organization_id}:{record.external_operation_ref}"


def _encryption_key(secret: str | None = None) -> bytes:
    material = (secret if secret is not None else settings.SECRET_KEY).encode("utf-8")
    return hashlib.sha256(FAKE_PROVIDER_EVIDENCE_KEY_PURPOSE.encode("utf-8") + b":" + material).digest()


def _chmod_private_dir(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class _file_lock:
    def __init__(self, path: Path, *, timeout_seconds: float = 5.0, stale_after_seconds: float = 30.0):
        self._path = path
        self._timeout = timeout_seconds
        self._stale_after = stale_after_seconds
        self._fd: int | None = None

    def __enter__(self):
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                self._fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                os.write(self._fd, f"{os.getpid()}:{time.time()}".encode("ascii"))
                os.fsync(self._fd)
                return self
            except FileExistsError:
                self._remove_stale_lock()
                if time.monotonic() >= deadline:
                    raise FakeCheckoutEvidenceStorageFailure("Fake provider evidence lock timeout")
                time.sleep(0.01)

    def __exit__(self, exc_type, exc, tb):
        if self._fd is not None:
            os.close(self._fd)
        self._path.unlink(missing_ok=True)

    def _remove_stale_lock(self) -> None:
        try:
            age = time.time() - self._path.stat().st_mtime
        except OSError:
            return
        if age <= self._stale_after:
            return
        try:
            self._path.unlink()
        except OSError:
            return
