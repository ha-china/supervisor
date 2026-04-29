"""Test discovery API."""

import logging
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from aiohttp.test_utils import TestClient
import pytest

from supervisor.addons.addon import App
from supervisor.const import AppState
from supervisor.coresys import CoreSys
from supervisor.discovery import Message

from tests.common import load_json_fixture
from tests.const import TEST_ADDON_SLUG


@pytest.mark.parametrize("api_client", ["local_ssh"], indirect=True)
async def test_api_discovery_forbidden(
    api_client: TestClient, caplog: pytest.LogCaptureFixture, install_app_ssh
):
    """Test app sending discovery message for an unregistered service."""
    caplog.clear()

    with caplog.at_level(logging.ERROR):
        resp = await api_client.post(
            "/discovery", json={"service": "mqtt", "config": {}}
        )

    assert resp.status == 403
    result = await resp.json()
    assert result["result"] == "error"
    assert (
        result["message"]
        == "Apps must list services they provide via discovery in their config!"
    )
    assert "Please report this to the maintainer of the app" in caplog.text


@pytest.mark.parametrize(
    "skip_state", [AppState.ERROR, AppState.STOPPED, AppState.STARTUP]
)
async def test_api_list_discovery(
    api_client_with_prefix: tuple[TestClient, str],
    coresys: CoreSys,
    install_app_ssh: App,
    skip_state: AppState,
):
    """Test listing discovery messages only returns ones for healthy services."""
    api_client, prefix = api_client_with_prefix
    with (
        patch(
            "supervisor.utils.common.read_json_or_yaml_file",
            return_value=load_json_fixture("discovery.json"),
        ),
        patch("supervisor.utils.common.Path.is_file", return_value=True),
    ):
        await coresys.discovery.read_data()

    await coresys.discovery.load()
    assert coresys.discovery.list_messages == [
        Message(app="core_mosquitto", service="mqtt", config=ANY, uuid=ANY),
        Message(app="local_ssh", service="adguard", config=ANY, uuid=ANY),
    ]

    install_app_ssh.state = AppState.STARTED
    resp = await api_client.get(f"{prefix}/discovery")
    assert resp.status == 200
    result = await resp.json()
    app_key = "app" if prefix == "/v2" else "addon"
    assert result["data"]["discovery"] == [
        {
            app_key: "local_ssh",
            "service": "adguard",
            "config": ANY,
            "uuid": ANY,
        }
    ]

    install_app_ssh.state = skip_state
    resp = await api_client.get(f"{prefix}/discovery")
    assert resp.status == 200
    result = await resp.json()
    assert result["data"]["discovery"] == []


@pytest.mark.parametrize("api_client", [TEST_ADDON_SLUG], indirect=True)
async def test_api_send_del_discovery(
    api_client: TestClient,
    coresys: CoreSys,
    install_app_ssh: App,
    websession: MagicMock,
):
    """Test adding and removing discovery."""
    install_app_ssh.data["discovery"] = ["test"]
    coresys.homeassistant.api._ensure_access_token = AsyncMock()  # pylint: disable=protected-access

    resp = await api_client.post("/discovery", json={"service": "test", "config": {}})
    assert resp.status == 200
    result = await resp.json()
    uuid = result["data"]["uuid"]
    coresys.websession.request.assert_called_once()
    assert coresys.websession.request.call_args.args[0] == "post"
    assert (
        coresys.websession.request.call_args.args[1]
        == f"http://172.30.32.1:8123/api/hassio_push/discovery/{uuid}"
    )
    assert coresys.websession.request.call_args.kwargs["json"] == {
        "addon": TEST_ADDON_SLUG,
        "service": "test",
        "uuid": uuid,
    }

    message = coresys.discovery.get(uuid)
    assert message.app == TEST_ADDON_SLUG
    assert message.service == "test"
    assert message.config == {}

    coresys.websession.request.reset_mock()
    resp = await api_client.delete(f"/discovery/{uuid}")
    assert resp.status == 200
    coresys.websession.request.assert_called_once()
    assert coresys.websession.request.call_args.args[0] == "delete"
    assert (
        coresys.websession.request.call_args.args[1]
        == f"http://172.30.32.1:8123/api/hassio_push/discovery/{uuid}"
    )
    assert coresys.websession.request.call_args.kwargs["json"] == {
        "addon": TEST_ADDON_SLUG,
        "service": "test",
        "uuid": uuid,
    }

    assert coresys.discovery.get(uuid) is None


@pytest.mark.parametrize("api_client", [TEST_ADDON_SLUG], indirect=True)
async def test_api_invalid_discovery(api_client: TestClient, install_app_ssh: App):
    """Test invalid discovery messages."""
    install_app_ssh.data["discovery"] = ["test"]

    resp = await api_client.post("/discovery", json={"service": "test"})
    assert resp.status == 400

    resp = await api_client.post("/discovery", json={"service": "test", "config": None})
    assert resp.status == 400


@pytest.mark.parametrize(
    ("method", "url"),
    [("get", "/discovery/bad"), ("delete", "/discovery/bad")],
)
async def test_discovery_not_found(
    api_client_with_prefix: tuple[TestClient, str], method: str, url: str
):
    """Test discovery not found error."""
    api_client, prefix = api_client_with_prefix
    resp = await api_client.request(method, f"{prefix}{url}")
    assert resp.status == 404
    resp = await resp.json()
    assert resp["message"] == "Discovery message not found"
