from unittest.mock import AsyncMock

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.elicitation import AcceptedElicitation
from mcp.server.elicitation import CancelledElicitation, DeclinedElicitation

import tools.configuration as mod
from constants import CONFIG_PREVIEW_MAX_CHARS
from models import CentralError, ConfigEnvelope, ConfigWriteResult
from tests.conftest import FakeMCP, make_ctx

VLAN_10 = {"vlan": 10, "name": "Staff", "description": "staff vlan"}
VLAN_20 = {"vlan": 20, "name": "Guest"}


def _resp(code: int, msg: object = None) -> dict:
    return {"code": code, "msg": msg, "headers": {}}


@pytest.fixture
def read_tools():
    fake = FakeMCP()
    mod.register(fake)
    return fake._tools


@pytest.fixture
def tools():
    fake = FakeMCP()
    mod.register(fake, enable_writes=True)
    return fake._tools


def _central_error(exc_info: pytest.ExceptionInfo[ToolError]) -> CentralError:
    return CentralError.model_validate_json(str(exc_info.value))


def _conn(ctx):
    return ctx.lifespan_context["conn"]


# --- registration -----------------------------------------------------------


def test_write_tool_is_opt_in(read_tools, tools):
    assert set(read_tools) == {"central_get_config"}
    assert set(tools) == {"central_get_config", "central_write_config"}


# --- central_get_config -----------------------------------------------------


async def test_get_collection_unwraps_bulk_key_and_drops_metadata(tools):
    ctx = make_ctx()
    _conn(ctx).command.return_value = _resp(
        200, {"layer2-vlan": [VLAN_10, VLAN_20], "metadata": {"count": 2}}
    )

    result = await tools["central_get_config"](ctx, resource="layer2-vlan")

    assert isinstance(result, ConfigEnvelope)
    assert result.items == [VLAN_10, VLAN_20]
    assert result.bulk_key == "layer2-vlan"
    assert result.resource == "layer2-vlan"
    assert result.meta.returned == 2
    _conn(ctx).command.assert_called_once_with(
        api_method="GET",
        api_path="network-config/v1alpha1/layer2-vlan",
        api_params=None,
    )


async def test_get_single_profile_quotes_name(tools):
    ctx = make_ctx()
    _conn(ctx).command.return_value = _resp(200, {**VLAN_10, "metadata": {}})

    result = await tools["central_get_config"](
        ctx, resource="/network-config/v1alpha1/wlan-ssids/", name="Corp WiFi/5G"
    )

    assert result.items == [VLAN_10]
    assert result.bulk_key is None
    assert result.resource == "wlan-ssids"
    assert (
        _conn(ctx).command.call_args.kwargs["api_path"]
        == "network-config/v1alpha1/wlan-ssids/Corp%20WiFi%2F5G"
    )


async def test_get_local_profile_sends_scope_params(tools):
    ctx = make_ctx()
    _conn(ctx).command.return_value = _resp(200, VLAN_10)

    await tools["central_get_config"](
        ctx, resource="layer2-vlan", name="10", scope_id=12345, persona="ACCESS_SWITCH"
    )

    assert _conn(ctx).command.call_args.kwargs["api_params"] == {
        "object_type": "LOCAL",
        "scope_id": 12345,
        "persona": "ACCESS_SWITCH",
        "view_type": "LOCAL",
    }


async def test_get_missing_profile_returns_empty_envelope(tools):
    ctx = make_ctx()
    _conn(ctx).command.return_value = _resp(404, {"message": "not found"})

    result = await tools["central_get_config"](ctx, resource="layer2-vlan", name="99")

    assert result.items == []
    assert result.meta.returned == 0


async def test_get_upstream_error_uses_central_error_contract(tools):
    ctx = make_ctx()
    _conn(ctx).command.return_value = _resp(403, {"message": "forbidden"})

    with pytest.raises(ToolError) as exc_info:
        await tools["central_get_config"](ctx, resource="layer2-vlan")

    error = _central_error(exc_info)
    assert error.code == "upstream_request_error"
    assert "reading configuration failed" in error.message


@pytest.mark.parametrize(
    "resource", ["", "../monitoring", "layer2-vlan?x=1", "a//b", "network-monitoring/v1/aps x"]
)
async def test_get_rejects_unsafe_resource(tools, resource):
    ctx = make_ctx()

    with pytest.raises(ToolError) as exc_info:
        await tools["central_get_config"](ctx, resource=resource)

    assert _central_error(exc_info).code == "validation_error"
    _conn(ctx).command.assert_not_called()


async def test_get_requires_scope_and_persona_together(tools):
    ctx = make_ctx()

    with pytest.raises(ToolError) as exc_info:
        await tools["central_get_config"](ctx, resource="layer2-vlan", scope_id=1)

    assert "provided together" in _central_error(exc_info).message
    _conn(ctx).command.assert_not_called()


# --- central_write_config: validation ---------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"action": "create"}, "requires a non-empty config"),
        ({"action": "update", "name": "10", "config": {}}, "requires a non-empty config"),
        ({"action": "replace", "config": VLAN_10}, "requires name"),
        ({"action": "delete"}, "requires name"),
    ],
)
async def test_write_validation_happens_before_any_call(tools, kwargs, fragment):
    ctx = make_ctx()
    ctx.elicit = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await tools["central_write_config"](ctx, resource="layer2-vlan", **kwargs)

    error = _central_error(exc_info)
    assert error.code == "validation_error"
    assert fragment in error.message
    _conn(ctx).command.assert_not_called()
    ctx.elicit.assert_not_called()


# --- central_write_config: confirmation -------------------------------------


@pytest.mark.parametrize("outcome", [DeclinedElicitation(), CancelledElicitation()])
async def test_write_declined_makes_no_change(tools, outcome):
    ctx = make_ctx()
    ctx.elicit = AsyncMock(return_value=outcome)
    _conn(ctx).command.return_value = _resp(200, VLAN_10)

    with pytest.raises(ToolError) as exc_info:
        await tools["central_write_config"](
            ctx,
            action="update",
            resource="layer2-vlan",
            name="10",
            config={"description": "new"},
        )

    assert "declined or cancelled" in _central_error(exc_info).message
    # Only the pre-write GET of the current profile was sent.
    methods = [c.kwargs["api_method"] for c in _conn(ctx).command.call_args_list]
    assert methods == ["GET"]


async def test_update_shows_current_and_body_then_patches(tools):
    ctx = make_ctx()
    ctx.elicit = AsyncMock(return_value=AcceptedElicitation(data={}))
    _conn(ctx).command.side_effect = [
        _resp(200, {**VLAN_10, "metadata": {"x": 1}}),
        _resp(200, {"message": "ok"}),
    ]

    result = await tools["central_write_config"](
        ctx,
        action="update",
        resource="layer2-vlan",
        name="10",
        config={"description": "new"},
        scope_id=42,
        persona="CORE_SWITCH",
    )

    approval_msg = ctx.elicit.call_args.args[0]
    assert "Confirm CONFIGURATION UPDATE" in approval_msg
    assert "PATCH network-config/v1alpha1/layer2-vlan/10" in approval_msg
    assert "scope 42 / persona CORE_SWITCH" in approval_msg
    assert '"description": "staff vlan"' in approval_msg
    assert '"description": "new"' in approval_msg
    assert "metadata" not in approval_msg

    write_call = _conn(ctx).command.call_args_list[1]
    assert write_call.kwargs == {
        "api_method": "PATCH",
        "api_path": "network-config/v1alpha1/layer2-vlan/10",
        "api_params": {"object_type": "LOCAL", "scope_id": 42, "persona": "CORE_SWITCH"},
        "api_data": {"description": "new"},
    }
    assert result == ConfigWriteResult(
        action="update",
        method="PATCH",
        path="network-config/v1alpha1/layer2-vlan/10",
        status_code=200,
        response={"message": "ok"},
    )


async def test_create_posts_without_prefetch(tools):
    ctx = make_ctx()
    ctx.elicit = AsyncMock(return_value=AcceptedElicitation(data={}))
    _conn(ctx).command.return_value = _resp(200, "")

    result = await tools["central_write_config"](
        ctx, action="create", resource="layer2-vlan", name="30", config={"vlan": 30}
    )

    _conn(ctx).command.assert_called_once_with(
        api_method="POST",
        api_path="network-config/v1alpha1/layer2-vlan/30",
        api_params=None,
        api_data={"vlan": 30},
    )
    assert "Current configuration" not in ctx.elicit.call_args.args[0]
    assert result.method == "POST"
    assert result.response is None


async def test_replace_uses_put(tools):
    ctx = make_ctx()
    ctx.elicit = AsyncMock(return_value=AcceptedElicitation(data={}))
    _conn(ctx).command.side_effect = [_resp(404, {}), _resp(200, VLAN_10)]

    result = await tools["central_write_config"](
        ctx, action="replace", resource="layer2-vlan", name="10", config=VLAN_10
    )

    assert "Current configuration: not found." in ctx.elicit.call_args.args[0]
    assert _conn(ctx).command.call_args.kwargs["api_method"] == "PUT"
    assert result.method == "PUT"


async def test_delete_by_name(tools):
    ctx = make_ctx()
    ctx.elicit = AsyncMock(return_value=AcceptedElicitation(data={}))
    _conn(ctx).command.side_effect = [_resp(200, VLAN_10), _resp(200, "")]

    result = await tools["central_write_config"](
        ctx, action="delete", resource="layer2-vlan", name="10"
    )

    assert _conn(ctx).command.call_args.kwargs == {
        "api_method": "DELETE",
        "api_path": "network-config/v1alpha1/layer2-vlan/10",
        "api_params": None,
        "headers": {"Accept": "*/*"},
    }
    assert result.status_code == 200


async def test_delete_missing_profile_never_prompts(tools):
    ctx = make_ctx()
    ctx.elicit = AsyncMock()
    _conn(ctx).command.return_value = _resp(404, {})

    with pytest.raises(ToolError) as exc_info:
        await tools["central_write_config"](
            ctx, action="delete", resource="layer2-vlan", name="99"
        )

    assert "Nothing to delete" in _central_error(exc_info).message
    ctx.elicit.assert_not_called()
    assert _conn(ctx).command.call_count == 1


async def test_body_based_delete_for_scope_maps(tools):
    ctx = make_ctx()
    ctx.elicit = AsyncMock(return_value=AcceptedElicitation(data={}))
    _conn(ctx).command.return_value = _resp(200, {})
    body = {
        "scope-map": [
            {"scope-name": "123", "persona": "CAMPUS_AP", "resource": "wlan-ssids/Corp"}
        ]
    }

    await tools["central_write_config"](
        ctx, action="delete", resource="scope-maps", config=body
    )

    _conn(ctx).command.assert_called_once_with(
        api_method="DELETE",
        api_path="network-config/v1alpha1/scope-maps",
        api_params=None,
        api_data=body,
    )


async def test_write_upstream_failure_uses_central_error_contract(tools):
    ctx = make_ctx()
    ctx.elicit = AsyncMock(return_value=AcceptedElicitation(data={}))
    _conn(ctx).command.return_value = _resp(400, {"message": "invalid vlan"})

    with pytest.raises(ToolError) as exc_info:
        await tools["central_write_config"](
            ctx, action="create", resource="layer2-vlan", config={"vlan": 5000}
        )

    error = _central_error(exc_info)
    assert error.code == "upstream_request_error"
    assert "running configuration create failed" in error.message
    assert "invalid vlan" in error.message


async def test_large_payload_preview_is_truncated(tools):
    ctx = make_ctx()
    ctx.elicit = AsyncMock(return_value=DeclinedElicitation())
    big = {"description": "x" * (CONFIG_PREVIEW_MAX_CHARS * 2)}

    with pytest.raises(ToolError):
        await tools["central_write_config"](
            ctx, action="create", resource="layer2-vlan", config=big
        )

    approval_msg = ctx.elicit.call_args.args[0]
    assert "truncated" in approval_msg
    assert len(approval_msg) < CONFIG_PREVIEW_MAX_CHARS + 1000
