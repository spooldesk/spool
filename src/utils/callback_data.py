"""Callback data parsing and generation utilities.

Provides a structured way to handle callback_data for inline keyboards.
Format: <scope>:<entity>:<action>:<payload>

where payload is JSON-encoded string to avoid issues with colons in values.
"""

import json
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class CallbackDataError(Exception):
    """Base exception for callback data parsing errors."""
    pass


class InvalidCallbackFormat(CallbackDataError):
    """Raised when callback_data format is invalid."""
    pass


def parse_callback_data(callback_data: str) -> Tuple[str, str, str, Dict[str, Any]]:
    """
    Parse callback_data into structured components.
    
    Format: <scope>:<entity>:<action>:<payload>
    where payload is JSON-encoded string.
    
    Args:
        callback_data: Raw callback_data string from Telegram
        
    Returns:
        Tuple of (scope, entity, action, payload_dict)
        
    Raises:
        InvalidCallbackFormat: If callback_data format is invalid
    """
    logger.debug(f"Parsing callback_data: {callback_data}")
    
    # Split into max 4 parts: scope, entity, action, payload
    parts = callback_data.split(":", 3)
    
    if len(parts) < 3:
        error_msg = f"Invalid callback_data format: expected at least 3 parts, got {len(parts)}"
        logger.error(f"{error_msg}. Raw data: {callback_data}")
        raise InvalidCallbackFormat(error_msg)
    
    scope = parts[0]
    entity = parts[1]
    action = parts[2]
    payload_str = parts[3] if len(parts) > 3 else "{}"
    
    # Validate that scope, entity, action are strings (not empty)
    if not scope or not entity or not action:
        error_msg = f"Invalid callback_data: scope, entity, action must be non-empty strings"
        logger.error(f"{error_msg}. scope={scope}, entity={entity}, action={action}")
        raise InvalidCallbackFormat(error_msg)
    
    # Parse payload as JSON (new format) with legacy fallback support
    try:
        payload = json.loads(payload_str) if payload_str else {}
        if not isinstance(payload, dict):
            # Legacy support:
            # old callback payload could be a scalar like "123" or "all".
            # Normalize it into a dict with common aliases used by handlers.
            legacy_payload: Dict[str, Any] = {"value": payload}

            if isinstance(payload, int):
                legacy_payload.update(
                    {
                        "i": payload,
                        "id": payload,
                        "u": payload,
                        "user_id": payload,
                        "t": payload,
                        "ticket_id": payload,
                        "p": payload,
                        "page": payload,
                    }
                )
            elif isinstance(payload, str):
                payload_stripped = payload.strip()
                legacy_payload.update(
                    {
                        "n": payload_stripped,
                        "team_name": payload_stripped,
                        "t": payload_stripped,
                        "target": payload_stripped,
                    }
                )
                if payload_stripped.isdigit():
                    numeric_value = int(payload_stripped)
                    legacy_payload.update(
                        {
                            "i": numeric_value,
                            "id": numeric_value,
                            "u": numeric_value,
                            "user_id": numeric_value,
                            "t": numeric_value,
                            "ticket_id": numeric_value,
                            "p": numeric_value,
                            "page": numeric_value,
                        }
                    )

            payload = legacy_payload
    except json.JSONDecodeError as e:
        # Legacy support for non-JSON payloads (e.g., "123" or "all")
        raw_payload = payload_str.strip()
        if not raw_payload:
            payload = {}
        else:
            payload = {"value": raw_payload}
            payload["n"] = raw_payload
            payload["team_name"] = raw_payload
            payload["t"] = raw_payload
            payload["target"] = raw_payload
            if raw_payload.isdigit():
                numeric_value = int(raw_payload)
                payload.update(
                    {
                        "i": numeric_value,
                        "id": numeric_value,
                        "u": numeric_value,
                        "user_id": numeric_value,
                        "t": numeric_value,
                        "ticket_id": numeric_value,
                        "p": numeric_value,
                        "page": numeric_value,
                    }
                )
            logger.warning(
                f"Parsed legacy non-JSON callback payload: '{payload_str}' "
                f"(scope={scope}, entity={entity}, action={action})"
            )
    
    logger.debug(
        f"Parsed callback_data: scope={scope}, entity={entity}, action={action}, "
        f"payload={payload}"
    )
    
    return scope, entity, action, payload


def build_callback_data(
    scope: str,
    entity: str,
    action: str,
    payload: Optional[Dict[str, Any]] = None
) -> str:
    """
    Build callback_data string from structured components.
    
    Args:
        scope: Scope identifier (e.g., "admin", "user")
        entity: Entity type (e.g., "ticket", "user", "team")
        action: Action name (e.g., "view", "assign", "close")
        payload: Optional dictionary with additional data
        
    Returns:
        Formatted callback_data string
        
    Raises:
        ValueError: If callback_data exceeds 64 bytes (Telegram limit)
    """
    if not scope or not entity or not action:
        raise ValueError("scope, entity, and action must be non-empty strings")
    
    # Validate that scope, entity, action don't contain colons
    if ":" in scope or ":" in entity or ":" in action:
        raise ValueError("scope, entity, and action cannot contain colons")
    
    payload = payload or {}
    
    # Optimize payload to reduce size: use short keys and minimize data
    optimized_payload = {}
    for key, value in payload.items():
        # Use short keys for common fields
        short_key = {
            "id": "i",
            "user_id": "u",
            "ticket_id": "t",
            "team_name": "n",
            "page": "p",
            "target": "t",
            "category": "c",
        }.get(key, key)
        
        # Convert long strings to IDs if possible
        if isinstance(value, str) and len(value) > 10:
            # Keep as is for now, but log warning if too long
            optimized_payload[short_key] = value
        else:
            optimized_payload[short_key] = value
    
    payload_str = json.dumps(optimized_payload, separators=(',', ':'))  # Compact JSON
    
    callback_data = f"{scope}:{entity}:{action}:{payload_str}"
    
    # Check Telegram's 64-byte limit
    if len(callback_data) > 64:
        logger.error(
            f"⚠️ WARNING: callback_data exceeds 64 bytes limit!\n"
            f"  - Length: {len(callback_data)} bytes\n"
            f"  - Data: {callback_data}\n"
            f"  - Scope: {scope}, Entity: {entity}, Action: {action}\n"
            f"  - Payload: {payload}"
        )
        # Try to reduce payload size
        if optimized_payload:
            # Remove non-essential fields
            minimal_payload = {}
            for key in ["i", "u", "t", "p"]:  # Only keep essential IDs
                if key in optimized_payload:
                    minimal_payload[key] = optimized_payload[key]
            payload_str = json.dumps(minimal_payload, separators=(',', ':'))
            callback_data = f"{scope}:{entity}:{action}:{payload_str}"
            
            if len(callback_data) > 64:
                raise ValueError(
                    f"callback_data still exceeds 64 bytes after optimization: "
                    f"{len(callback_data)} bytes. Data: {callback_data}"
                )
    
    logger.debug(
        f"Built callback_data: {callback_data} ({len(callback_data)} bytes) "
        f"(scope={scope}, entity={entity}, action={action}, payload={payload})"
    )
    
    return callback_data


def safe_parse_callback_data(callback_data: str) -> Optional[Tuple[str, str, str, Dict[str, Any]]]:
    """
    Safely parse callback_data, returning None on error instead of raising.
    
    Useful for handlers that need to handle both old and new formats.
    
    Args:
        callback_data: Raw callback_data string
        
    Returns:
        Tuple of (scope, entity, action, payload_dict) or None if parsing fails
    """
    try:
        return parse_callback_data(callback_data)
    except InvalidCallbackFormat as e:
        logger.warning(f"Failed to parse callback_data: {e}. Raw data: {callback_data}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error parsing callback_data: {e}. Raw data: {callback_data}")
        return None


def get_payload_value(payload: Dict[str, Any], key: str, default: Any = None, type_hint: type = None) -> Any:
    """
    Safely get value from payload with optional type conversion.
    
    Args:
        payload: Payload dictionary
        key: Key to retrieve
        default: Default value if key not found
        type_hint: Optional type to convert value to (e.g., int, str)
        
    Returns:
        Value from payload, optionally converted to type_hint
    """
    if key not in payload:
        return default
    
    value = payload[key]
    
    if type_hint is None:
        return value
    
    try:
        return type_hint(value)
    except (ValueError, TypeError) as e:
        logger.warning(f"Failed to convert payload['{key}']={value} to {type_hint}: {e}")
        return default
