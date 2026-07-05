import copy
from typing import Dict, Any

def track_event(event_name: str, properties: Dict[str, Any]) -> None:
    """
    Standard untrusted telemetry logger tracking raw payloads.
    """
    pass

def track_event_safe(event_name: str, properties: Dict[str, Any]) -> None:
    """
    Safe telemetry wrapper scrubbing granular location fields to safeguard customer PII.
    Allows non-PII metrics: city, state_province, country_code.
    """
    scrub_keys = {
        "address_line1", "address_line2", "postal_code", "coordinates", "lat", "lng", "formatted_address", "ip_address",
        "google_place_id", "latitude", "longitude", "maps_url", "embed_url"
    }
    
    # Deep copy to prevent mutating local state parameters
    safe_props = copy.deepcopy(properties)
    for key in scrub_keys:
        safe_props.pop(key, None)
        
    track_event(event_name, safe_props)


def sentry_before_send(event: Dict[str, Any], hint: Dict[str, Any]) -> Dict[str, Any]:
    """
    Sentry before_send callback hook. Recursively scrubs granular address PII fields
    from Sentry payload dictionaries, exceptions, local contexts, and trace breadcrumbs.
    """
    scrub_keys = {
        "address_line1", "address_line2", "postal_code", "formatted_address", "coordinates",
        "google_place_id", "latitude", "longitude", "maps_url", "embed_url"
    }
    
    def recursive_scrub(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                k: ("[REDACTED]" if k in scrub_keys else recursive_scrub(v))
                for k, v in node.items()
            }
        elif isinstance(node, list):
            return [recursive_scrub(item) for item in node]
        return node

    # Scrub entire event dictionary recursively
    return recursive_scrub(event)
