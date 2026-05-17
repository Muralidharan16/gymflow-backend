import re
import unicodedata
import uuid

def generate_slug(text: str) -> str:
    """
    Generate a URL-safe slug from text.
    Example: "Titan Fitness" -> "titan-fitness"
    """
    # Convert to lowercase and normalize unicode characters
    text = unicodedata.normalize('NFKD', text.lower()).encode('ascii', 'ignore').decode('ascii')
    
    # Replace non-alphanumeric characters with hyphens
    text = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    
    # If text is empty after cleaning, use a fallback
    if not text:
        return str(uuid.uuid4())[:8]
        
    return text
