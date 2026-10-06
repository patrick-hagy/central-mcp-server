import asyncio
from typing import Any, Literal

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.elicitation import AcceptedElicitation
from mcp import McpError

from constants import CONFIG_PERSONA, MAX_RESPONSE_ITEMS
from models import ConfigEnvelope, ConfigWriteResult
from tools import DESTRUCTIVE, READ_ONLY
from utils.common import api_context
from utils.configuration import (
    WRITE_METHODS,
    build_config_path,
    build_confirmation,
    ensure_success,
    local_params,
    normalize_resource,
    read_params,
    split_config_payload,
    strip_metadata,
    validate_write,
)
from utils.envelope import build_envelope, raise_central_error


async def _get_config_impl(
    ctx: Context,
    resource: str,
    name: str | None,
    scope_id: int | None,
    persona: str | None,
) -> ConfigEnvelope:
    """Read configuration behind the registered tool's error boundary."""
    resource = normalize_resource(resource)
    local = local_params(scope_id, persona)
    path = build_config_path(resource, name)

    async with api_context(ctx) as conn:
        response = await asyncio.to_thread(
            conn.command,
            api_method="GET",
            api_path=path,
            api_params=read_params(local),
        )

    if response.get("code") == 404:
        items, bulk_key = [], None
    else:
        items, bulk_key = split_config_payload(
            ensure_success(response), collection=name is None
        )

    envelope = build_envelope(
        ConfigEnvelope,
        items,
        total=len(items),
        ceiling=MAX_RESPONSE_ITEMS,
        response_format="detailed",
    )
    envelope.resource = resource
    envelope.bulk_key = bulk_key
    return envelope


async def _write_config_impl(
    ctx: Context,
    action: str,
    resource: str,
    name: str | None,
    config: dict[str, Any] | None,
    scope_id: int | None,
    persona: str | None,
) -> ConfigWriteResult:
    """Confirm and send a configuration write behind the tool's error boundary."""
    resource = normalize_resource(resource)
    local = local_params(scope_id, persona)
    validate_write(action, name, config)
    path = build_config_path(resource, name)
    method = WRITE_METHODS[action]

    async with api_context(ctx) as conn:
        current = None
        current_found: bool | None = None
        if name is not None and action != "create":
            response = await asyncio.to_thread(
                conn.command,
                api_method="GET",
                api_path=path,
                api_params=read_params(local),
            )
            if response.get("code") == 404:
                current_found = False
            else:
                current = strip_metadata(ensure_success(response))
                current_found = True
            if action == "delete" and not current_found:
                raise ValueError(f"Nothing to delete: {path} was not found in Central.")

        approval_msg = build_confirmation(
            action, method, path, local, current, current_found, config
        )
        elicit_result = await ctx.elicit(approval_msg, response_type=None)
        if not isinstance(elicit_result, AcceptedElicitation):
            raise ValueError(
                "Configuration write was declined or cancelled by the user."
            )

        command_kwargs: dict[str, Any] = {
            "api_method": method,
            "api_path": path,
            "api_params": local,
        }
        if config:
            command_kwargs["api_data"] = config
        elif method == "DELETE":
            # Central's profile DELETE returns no JSON body.
            command_kwargs["headers"] = {"Accept": "*/*"}
        response = await asyncio.to_thread(conn.command, **command_kwargs)

    body = ensure_success(response)
    return ConfigWriteResult(
        action=action,
        method=method,
        path=path,
        status_code=response["code"],
        response=body if body not in ("", {}) else None,
    )


def register(mcp: FastMCP, *, enable_writes: bool = False) -> None:
    """Register configuration tools; the write tool only when writes are enabled."""

    @mcp.tool(annotations=READ_ONLY)
    async def central_get_config(
        ctx: Context,
        resource: str,
        name: str | None = None,
        scope_id: int | None = None,
        persona: CONFIG_PERSONA | None = None,
    ) -> ConfigEnvelope:
        """Read Central configuration profiles from the network-config API.

        Use to bring existing configuration into context before reviewing or changing it.
        resource is the path after network-config/v1alpha1/ (e.g. layer2-vlan, wlan-ssids, scope-maps); name selects one profile, otherwise the collection is returned.
        Cross-field rule: scope_id and persona go together and read the scope-level (local) profile instead of the shared library one.
        Returns ConfigEnvelope with raw config objects and the collection bulk_key; a missing profile returns no items.
        """
        try:
            return await _get_config_impl(ctx, resource, name, scope_id, persona)
        except (ToolError, McpError):
            raise
        except Exception as exc:
            raise_central_error(exc, "reading configuration")

    if not enable_writes:
        return

    @mcp.tool(annotations=DESTRUCTIVE)
    async def central_write_config(
        ctx: Context,
        action: Literal["create", "update", "replace", "delete"],
        resource: str,
        name: str | None = None,
        config: dict[str, Any] | None = None,
        scope_id: int | None = None,
        persona: CONFIG_PERSONA | None = None,
    ) -> ConfigWriteResult:
        """Create, update, replace, or delete a Central configuration profile after explicit user confirmation.

        Use only when the user asks to change configuration; read the current profile with central_get_config first.
        action maps to POST/PATCH/PUT/DELETE on network-config/v1alpha1/<resource>[/<name>]; config is the JSON body using Central's API field names.
        Cross-field rules: create/update/replace need config; replace needs name; delete needs name or config; scope_id and persona go together for a local profile.
        Returns ConfigWriteResult; decline or cancellation is an error with no change.
        """
        try:
            return await _write_config_impl(
                ctx, action, resource, name, config, scope_id, persona
            )
        except (ToolError, McpError):
            raise
        except Exception as exc:
            raise_central_error(exc, f"running configuration {action}")
