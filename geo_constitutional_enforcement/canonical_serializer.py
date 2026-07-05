"""
geo_constitutional_enforcement/canonical_serializer.py

Guarantees deterministic serialization for replay snapshot hashing.
Implements the rules defined in constitutional/canonical_serialization.yaml.
"""
import json
import hashlib
import unicodedata
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

# Matches Numeric(9,6) precision in app/models/geo.py
DECIMAL_PRECISION = 6

def normalize_unicode(s: str) -> str:
    """
    NFC normalization: Unicode Canonical Decomposition followed by Composition.
    Ensures identical byte sequences for equivalent characters (e.g., é).
    """
    return unicodedata.normalize("NFC", s)

def serialize_decimal(value: Decimal | float | None) -> str | None:
    """
    Fixed-point string representation with exact precision.
    Prevents float64 precision drift across environments.
    """
    if value is None:
        return None
    
    # Quantize forces exactly DECIMAL_PRECISION decimal places, rounding halves up
    # e.g., Decimal("1.5") -> Decimal("1.500000")
    d = Decimal(str(value)).quantize(
        Decimal(f"0.{'0' * DECIMAL_PRECISION}"), 
        rounding=ROUND_HALF_UP
    )
    return str(d)

def _default_handler(obj: Any) -> Any:
    """Custom JSON encoder for non-standard types."""
    if isinstance(obj, Decimal):
        return serialize_decimal(obj)
    # We don't handle datetime here; we expect the caller (or pydantic) 
    # to serialize datetimes to ISO8601 strings before canonical JSON serialization,
    # as defined in our contract.
    raise TypeError(f"Not serializable via canonical_serializer: {type(obj)}")

def canonical_json(obj: Any) -> str:
    """
    Produces deterministic JSON per canonical_serialization.yaml:
    - Sorted keys
    - No whitespace/indentation
    - Preserves ASCII/Unicode directly (ensure_ascii=False)
    """
    # First normalize strings deeply
    obj = _normalize_strings_deeply(obj)
    
    return json.dumps(
        obj, 
        sort_keys=True, 
        ensure_ascii=False,
        separators=(",", ":"), 
        default=_default_handler
    )

def _normalize_strings_deeply(obj: Any) -> Any:
    """Recursively apply NFC normalization to all string values."""
    if isinstance(obj, str):
        return normalize_unicode(obj)
    elif isinstance(obj, dict):
        return {k: _normalize_strings_deeply(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_normalize_strings_deeply(item) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(_normalize_strings_deeply(item) for item in obj)
    return obj

def canonical_hash(obj: Any) -> str:
    """
    Computes SHA-256 of canonical JSON.
    This is the fundamental primitive for replay equivalence validation.
    """
    serialized = canonical_json(obj).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()
