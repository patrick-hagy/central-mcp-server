import json
import re
from typing import Any, Literal
from urllib.parse import quote

from constants import CONFIG_API_PREFIX, CONFIG_PREVIEW_MAX_CHARS

# One or more slash-separated path segments, e.g. "layer2-vlan" or "scope-maps".
_RESOURCE_PATTERN = re.compile(
    r"[a-z0-9][a-z0-9_-]*(?:/[a-z0-9][a-z0-9_-]*)*", re.IGNORECASE
)
WRITE_METHODS: dict[str, Literal["POST", "PATCH", "PUT", "DELETE"]] = {
    "create": "POST",
    "update": "PATCH",
    "replace": "PUT",
    "delete": "DELETE",
}


def normalize_resource(resource: str) -> str:
    """Return the resource path relative to the configuration API prefix."""
    value = resource.strip().strip("/")
    if value.startswith(CONFIG_API_PREFIX):
        value = value[len(CONFIG_API_PREFIX) :]
    if not _RESOURCE_PATTERN.fullmatch(value):
        raise ValueError(
            f"Invalid resource '{resource}'. Pass the path after "
            f"{CONFIG_API_PREFIX}, e.g. 'layer2-vlan' or 'scope-maps'."
        )
    return value


def build_config_path(resource: str, name: str | None) -> str:
    """Build the configuration API path for a resource and optional profile name."""
    path = f"{CONFIG_API_PREFIX}{resource}"
    if name is None:
        return path
    if not name.strip():
        raise ValueError("name must not be empty when provided.")
    return f"{path}/{quote(name, safe='')}"


def local_params(scope_id: int | None, persona: str | None) -> dict | None:
    """Return query parameters selecting a local (scope-level) profile."""
    if scope_id is None and persona is None:
        return None
    if scope_id is None or persona is None:
        raise ValueError(
            "scope_id and persona must be provided together for a local profile."
        )
    return {"object_type": "LOCAL", "scope_id": scope_id, "persona": persona}


def read_params(local: dict | None) -> dict | None:
    """Add the view type Central requires when reading a local profile."""
    return {**local, "view_type": "LOCAL"} if local else None


def ensure_success(response: dict) -> Any:
    """Return the response body, raising for any non-2xx status."""
    code = response.get("code")
    if not isinstance(code, int) or not 200 <= code < 300:
        raise RuntimeError(f"API returned {code}: {response.get('msg')}")
    return response.get("msg")


def strip_metadata(payload: Any) -> Any:
    """Drop Central's response metadata block from a configuration body."""
    if isinstance(payload, dict):
        return {k: v for k, v in payload.items() if k != "metadata"}
    return payload


def _as_object(value: Any) -> dict[str, Any]:
    """Wrap a non-object list entry so every envelope item is a JSON object."""
    return value if isinstance(value, dict) else {"value": value}


def split_config_payload(
    payload: Any, *, collection: bool
) -> tuple[list[dict[str, Any]], str | None]:
    """Turn a configuration GET body into envelope items plus its bulk key.

    Collection reads come back wrapped under a single list-valued key (for
    example ``{"layer2-vlan": [...]}``); that key is reported so callers can
    reuse it when writing.  Single-profile reads are returned as one item.
    """
    payload = strip_metadata(payload)
    if payload in (None, "", {}):
        return [], None
    if isinstance(payload, list):
        return [_as_object(item) for item in payload], None
    if (
        collection
        and isinstance(payload, dict)
        and len(payload) == 1
        and isinstance(next(iter(payload.values())), list)
    ):
        key, values = next(iter(payload.items()))
        return [_as_object(item) for item in values], key
    return [_as_object(payload)], None


def _json_preview(value: Any) -> str:
    """Render JSON for a confirmation prompt, truncated to a readable size."""
    text = json.dumps(value, indent=2, sort_keys=True, default=str)
    if len(text) <= CONFIG_PREVIEW_MAX_CHARS:
        return text
    return (
        text[:CONFIG_PREVIEW_MAX_CHARS]
        + f"\n... (truncated; {len(text)} characters total)"
    )


def validate_write(
    action: str, name: str | None, config: dict[str, Any] | None
) -> None:
    """Enforce the per-action body and name requirements."""
    if config is not None and not isinstance(config, dict):
        raise ValueError("config must be a JSON object.")
    if action in ("create", "update", "replace") and not config:
        raise ValueError(f"action='{action}' requires a non-empty config object.")
    if action == "replace" and name is None:
        raise ValueError("action='replace' requires name.")
    if action == "delete" and name is None and not config:
        raise ValueError(
            "action='delete' requires name, or a config body for body-based deletes "
            "such as scope-maps."
        )


def build_confirmation(
    action: str,
    method: str,
    path: str,
    local: dict | None,
    current: Any,
    current_found: bool | None,
    config: dict[str, Any] | None,
) -> str:
    """Build the elicitation message describing the pending configuration write."""
    target = (
        f"local profile for scope {local['scope_id']} / persona {local['persona']}"
        if local
        else "shared (library) profile"
    )
    lines = [
        f"Confirm CONFIGURATION {action.upper()} in Central",
        f"{method} {path}",
        f"Target: {target}",
        "WARNING: This changes live configuration. Devices using this profile "
        "may be re-provisioned.",
    ]
    if current_found is False:
        lines.append("\nCurrent configuration: not found.")
    elif current_found:
        lines.extend(["\nCurrent configuration:", _json_preview(current)])
    if config:
        lines.extend(["\nRequest body:", _json_preview(config)])
    lines.append("\nAccept to proceed. Decline or cancel to abort.")
    return "\n".join(lines)
