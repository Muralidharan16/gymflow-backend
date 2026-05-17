from cryptography.fernet import Fernet
from app.core.config import settings
import base64

# Use a static key from settings for consistency across restarts
# In production, this should be a 32-byte URL-safe base64-encoded key
# If settings.SECRET_KEY is used, we ensure it is padded/truncated to 32 bytes
def get_encryption_key():
    # Use the first 32 chars of SECRET_KEY as the Fernet key
    key = settings.SECRET_KEY[:32].ljust(32, '0').encode()
    return base64.urlsafe_b64encode(key)

fernet = Fernet(get_encryption_key())

def encrypt_data(data: str) -> str:
    if not data:
        return ""
    return fernet.encrypt(data.encode()).decode()

def decrypt_data(encrypted_data: str) -> str:
    if not encrypted_data:
        return ""
    return fernet.decrypt(encrypted_data.encode()).decode()

def mask_id_number(id_number: str) -> str:
    """Mask ID number showing only last 4 digits (e.g. XXXXXX1234)"""
    if len(id_number) <= 4:
        return id_number
    return "X" * (len(id_number) - 4) + id_number[-4:]
