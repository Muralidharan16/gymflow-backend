import re
from typing import Optional


def normalize_phone(phone: str | None) -> str | None:
    """
    Normalize Indian phone number to 10-digit format.
    
    Handles:
    - Removal of +91 prefix
    - Removal of spaces, dashes, parentheses
    - Removal of leading 0
    - Validation of 10 digits
    
    Args:
        phone: Raw phone number string or None
    
    Returns:
        Normalized 10-digit string, or None if input is None/empty
    
    Raises:
        ValueError: If phone number is invalid (not 10 digits after cleaning)
    """
    if not phone:
        return None
    
    # Remove common separators
    cleaned = re.sub(r'[\s\-\(\)]', '', phone.strip())
    
    # Remove +91 or 91 prefix (case: +91XXXXXXXXXX, 91XXXXXXXXXX)
    cleaned = re.sub(r'^\+?91', '', cleaned)
    
    # Remove leading 0 if present
    cleaned = re.sub(r'^0+', '', cleaned)
    
    # Final validation: must be exactly 10 digits
    if not cleaned.isdigit() or len(cleaned) != 10:
        raise ValueError(f"Invalid phone number: {phone} (normalized to {cleaned})")
    
    return cleaned