import asyncio
from datetime import datetime, timezone
import logging

import pytest

from zigpy import device, endpoint
import zigpy.application
import zigpy.exceptions
from zigpy.profiles import zha
import zigpy.state
import zigpy.types as t
from zigpy.zcl.clusters.general import Basic
import zigpy.zcl.foundation as foundation
from zigpy.zdo import types as zdo_t

from .async_mock import ANY, AsyncMock, MagicMock, int_sentinel, patch, sentinel


@pytest.fixture
def dev(monkeypatch, app_mock):
    monkeypatch.setattr(device, "APS_REPLY_TIMEOUT_EXTENDED", 0.1)
    ieee = t.EUI64(map(t.uint8_t, [0, 1, 2, 3, 4, 5, 6, 7]))

    dev = device.Device(app_mock, ieee, 65535)
    node_desc = zdo_t.NodeDescriptor(1, 1, 1, 4, 5, 6, 7, 8)
    with patch.object(
        dev.zdo, "Node_Desc_req", new=AsyncMock(return_value=(0, 0xFFFF, node_desc))
    ):
        yield dev


async def test_initialize(monkeypatch, dev):
    async def mockrequest(nwk, tries=None, delay=None):
        return [0, None, [0, 1, 2, 3, 4]]

    async def mockepinit(self, *args, **kwargs):
        self.status = endpoint.Status.ZDO_INIT
        self.add_input_cluster(Basic.cluster_id)

    async def mock_ep_get_model_info(self):
        if self.endpoint_id == 1:
            return None, None
        elif self.endpoint_id == 2:
            return "Model", None
        elif self.endpoint_id == 3:
            return None, "Manufacturer"
        else:
            return "Model2", "Manufacturer2"

    monkeypatch.setattr(endpoint.Endpoint, "initialize", mockepinit)
    monkeypatch.setattr(endpoint.Endpoint, "get_model_info", mock_ep_get_model_info)
    dev.zdo.Active_EP_req = mockrequest
    await dev.initialize()

    assert dev.endpoints[0] is dev.zdo
    assert 1 in dev.endpoints
    assert 2 in dev.endpoints
    assert 3 in dev.endpoints
    assert 4 in dev.endpoints
    assert dev._application.device_initialized.call_count == 1
    assert dev.is_initialized

    # First one for each is chosen
    assert dev.model == "Model"
    assert dev.manufacturer == "Manufacturer"

    dev.schedule_initialize()
    assert dev._application.device_initialized.call_count == 2

    await dev.initialize()
    assert dev._application.device_initialized.call_count == 3


async def test_initialize_fail(dev):
    async def mockrequest(nwk, tries=None, delay=None):
        return [1, dev.nwk, []]

    dev.zdo.Active_EP_req = mockrequest
    await dev.initialize()

    assert not dev.is_initialized
    assert not dev.has_non_zdo_endpoints


@patch("zigpy.device.Device.get_node_descriptor", AsyncMock())
async def test_initialize_ep_failed(monkeypatch, dev):
    async def mockrequest(req, nwk, tries=None, delay=None):
        return [0, None, [1, 2]]

    async def mockepinit(self):
        raise AttributeError

    monkeypatch.setattr(endpoint.Endpoint, "initialize", mockepinit)

    dev.zdo.request = mockrequest
    await dev.initialize()

    assert not dev.is_initialized
    assert dev.application.listener_event.call_count == 1
    assert dev.application.listener_event.call_args[0][0] == "device_init_failure"


async def test_request(dev):
    seq = int_sentinel.tsn

    async def mock_req(*args, **kwargs):
        dev._pending[seq].result.set_result(sentinel.result)

    dev.application.send_packet = AsyncMock(side_effect=mock_req)
    r = await dev.request(1, 2, 3, 3, seq, b"")
    assert r is sentinel.result
    assert dev._application.send_packet.call_count == 1


async def test_request_without_reply(dev):
    seq = int_sentinel.tsn

    async def mock_req(*args, **kwargs):
        dev._pending[seq].result.set_result(sentinel.result)

    dev.application.send_packet = AsyncMock(side_effect=mock_req)
    r = await dev.request(1, 2, 3, 3, seq, b"", expect_reply=False)
    assert r is None
    assert dev._application.send_packet.call_count == 1


async def test_failed_request(dev):
    assert dev.last_seen is None
    dev._application.send_packet = AsyncMock(
        side_effect=zigpy.exceptions.DeliveryError("Uh oh")
    )
    with pytest.raises(zigpy.exceptions.DeliveryError):
        await dev.request(1, 2, 3, 4, 5, b"")
    assert dev.last_seen is None


def test_skip_configuration(dev):
    assert dev.skip_configuration is False
    dev.skip_configuration = True
    assert dev.skip_configuration is True


def test_radio_details(dev):
    dev.radio_details(1, 2)
    assert dev.lqi == 1
    assert dev.rssi == 2

    dev.radio_details(lqi=3)
    assert dev.lqi == 3
    assert dev.rssi == 2

    dev.radio_details(rssi=4)
    assert dev.lqi == 3
    assert dev.rssi == 4


async def test_handle_message_read_report_conf(dev):
    ep = dev.add_endpoint(3)
    ep.add_input_cluster(0x702)
    tsn = 0x56
    req_mock = MagicMock()
    dev._pending[tsn] = req_mock

    # Read Report Configuration Success
    rsp = dev.handle_message(
        0x104,  # profile
        0x702,  # cluster
        3,  # source EP
        3,  # dest EP
        b"\x18\x56\x09\x00\x00\x00\x00\x25\x1e\x00\x84\x03\x01\x02\x03\x04\x05\x06",  # message
    )
    # Returns decoded msg when response is not pending, None otherwise
    assert rsp is None
    assert req_mock.result.set_result.call_count == 1
    cfg_sup1 = req_mock.result.set_result.call_args[0][0].attribute_configs[0]
    assert isinstance(cfg_sup1, zigpy.zcl.foundation.AttributeReportingConfigWithStatus)
    assert cfg_sup1.status == zigpy.zcl.foundation.Status.SUCCESS
    assert cfg_sup1.config.direction == 0
    assert cfg_sup1.config.attrid == 0
    assert cfg_sup1.config.datatype == 0x25
    assert cfg_sup1.config.min_interval == 30
    assert cfg_sup1.config.max_interval == 900
    assert cfg_sup1.config.reportable_change == 0x060504030201

    # Unsupported attributes
    tsn2 = 0x5B
    req_mock2 = MagicMock()
    dev._pending[tsn2] = req_mock2
    rsp2 = dev.handle_message(
        0x104,  # profile
        0x702,  # cluster
        3,  # source EP
        3,  # dest EP
        b"\x18\x5b\x09\x86\x00\x00\x00\x86\x00\x12\x00\x86\x00\x00\x04",  # message 3x("Unsupported attribute" response)
    )
    # Returns decoded msg when response is not pending, None otherwise
    assert rsp2 is None
    cfg_unsup1, cfg_unsup2, cfg_unsup3 = req_mock2.result.set_result.call_args[0][
        0
    ].attribute_configs
    assert (
        cfg_unsup1.status
        == cfg_unsup2.status
        == cfg_unsup3.status
        == zigpy.zcl.foundation.Status.UNSUPPORTED_ATTRIBUTE
    )
    assert cfg_unsup1.config.direction == 0x00 and cfg_unsup1.config.attrid == 0x0000
    assert cfg_unsup2.config.direction == 0x00 and cfg_unsup2.config.attrid == 0x0012
    assert cfg_unsup3.config.direction == 0x00 and cfg_unsup3.config.attrid == 0x0400

    # One supported, one unsupported
    tsn3 = 0x5C
    req_mock3 = MagicMock()
    dev._pending[tsn3] = req_mock3
    rsp3 = dev.handle_message(
        0x104,  # profile
        0x702,  # cluster
        3,  # source EP
        3,  # dest EP
        b"\x18\x5c\x09\x86\x00\x00\x00\x00\x00\x00\x00\x25\x1e\x00\x84\x03\x01\x02\x03\x04\x05\x06",
    )
    assert rsp3 is None
    cfg_unsup4, cfg_sup2 = req_mock3.result.set_result.call_args[0][0].attribute_configs
    assert cfg_unsup4.status == zigpy.zcl.foundation.Status.UNSUPPORTED_ATTRIBUTE
    assert cfg_sup2.status == zigpy.zcl.foundation.Status.SUCCESS
    assert cfg_sup2.serialize() == cfg_sup1.serialize()


async def test_handle_message_deserialize_error(dev):
    ep = dev.add_endpoint(3)
    dev.deserialize = MagicMock(side_effect=ValueError)
    ep.handle_message = MagicMock()
    dev.handle_message(99, 98, 3, 3, b"abcd")
    assert ep.handle_message.call_count == 0


def test_endpoint_getitem(dev):
    ep = dev.add_endpoint(3)
    assert dev[3] is ep

    with pytest.raises(KeyError):
        dev[1]


async def test_broadcast(app_mock):
    app_mock.state.node_info.ieee = t.EUI64.convert("08:09:0A:0B:0C:0D:0E:0F")

    (profile, cluster, src_ep, dst_ep, data) = (
        zha.PROFILE_ID,
        1,
        2,
        3,
        b"\x02\x01\x00",
    )
    await device.broadcast(app_mock, profile, cluster, src_ep, dst_ep, 0, 0, 123, data)

    assert app_mock.send_packet.call_count == 1
    packet = app_mock.send_packet.mock_calls[0].args[0]

    assert packet.profile_id == profile
    assert packet.cluster_id == cluster
    assert packet.src_ep == src_ep
    assert packet.dst_ep == dst_ep
    assert packet.data.serialize() == data


async def _get_node_descriptor(dev, zdo_success=True, request_success=True):
    async def mockrequest(nwk, tries=None, delay=None):
        if not request_success:
            raise asyncio.TimeoutError

        status = 0 if zdo_success else 1
        return [status, nwk, zdo_t.NodeDescriptor.deserialize(b"abcdefghijklm")[0]]

    dev.zdo.Node_Desc_req = MagicMock(side_effect=mockrequest)
    return await dev.get_node_descriptor()


async def test_get_node_descriptor(dev):
    nd = await _get_node_descriptor(dev, zdo_success=True, request_success=True)

    assert nd is not None
    assert isinstance(nd, zdo_t.NodeDescriptor)
    assert dev.zdo.Node_Desc_req.call_count == 1


async def test_get_node_descriptor_no_reply(dev):
    with pytest.raises(asyncio.TimeoutError):
        await _get_node_descriptor(dev, zdo_success=True, request_success=False)

    assert dev.zdo.Node_Desc_req.call_count == 1


async def test_get_node_descriptor_fail(dev):
    with pytest.raises(zigpy.exceptions.InvalidResponse):
        await _get_node_descriptor(dev, zdo_success=False, request_success=True)

    assert dev.zdo.Node_Desc_req.call_count == 1


async def test_add_to_group(dev, monkeypatch):
    grp_id, grp_name = 0x1234, "test group 0x1234"
    epmock = MagicMock(spec_set=endpoint.Endpoint)
    monkeypatch.setattr(endpoint, "Endpoint", MagicMock(return_value=epmock))
    epmock.add_to_group = AsyncMock()

    dev.add_endpoint(3)
    dev.add_endpoint(4)

    await dev.add_to_group(grp_id, grp_name)
    assert epmock.add_to_group.call_count == 2
    assert epmock.add_to_group.call_args[0][0] == grp_id
    assert epmock.add_to_group.call_args[0][1] == grp_name


async def test_remove_from_group(dev, monkeypatch):
    grp_id = 0x1234
    epmock = MagicMock(spec_set=endpoint.Endpoint)
    monkeypatch.setattr(endpoint, "Endpoint", MagicMock(return_value=epmock))
    epmock.remove_from_group = AsyncMock()

    dev.add_endpoint(3)
    dev.add_endpoint(4)

    await dev.remove_from_group(grp_id)
    assert epmock.remove_from_group.call_count == 2
    assert epmock.remove_from_group.call_args[0][0] == grp_id


async def test_schedule_group_membership(dev, caplog):
    """Test preempting group membership scan."""

    p1 = patch.object(dev, "group_membership_scan", new=AsyncMock())
    caplog.set_level(logging.DEBUG)
    with p1 as scan_mock:
        dev.schedule_group_membership_scan()
        await asyncio.sleep(0)
        assert scan_mock.call_count == 1
        assert scan_mock.await_count == 1
        assert not [r for r in caplog.records if r.name != "asyncio"]

        scan_mock.reset_mock()
        dev.schedule_group_membership_scan()
        dev.schedule_group_membership_scan()
        await asyncio.sleep(0)
        assert scan_mock.await_count == 1
        assert "Cancelling old group rescan" in caplog.text


async def test_group_membership_scan(dev):
    ep = dev.add_endpoint(1)
    ep.status = endpoint.Status.ZDO_INIT

    with patch.object(ep, "group_membership_scan", new=AsyncMock()):
        await dev.group_membership_scan()
        assert ep.group_membership_scan.await_count == 1


def test_device_manufacture_id_override(dev):
    """Test manufacturer id override."""

    assert dev.manufacturer_id is None
    assert dev.manufacturer_id_override is None

    dev.node_desc = zdo_t.NodeDescriptor(1, 64, 142, 4153, 82, 255, 0, 255, 0)
    assert dev.manufacturer_id == 4153

    dev.manufacturer_id_override = 2345
    assert dev.manufacturer_id == 2345

    dev.node_desc = None
    assert dev.manufacturer_id == 2345


def test_device_name(dev):
    """Test device name property."""

    assert dev.nwk == 0xFFFF
    assert dev.name == "0xFFFF"


def test_device_last_seen(dev, monkeypatch):
    """Test the device last_seen property handles updates and broadcasts events."""

    monkeypatch.setattr(dev, "listener_event", MagicMock())
    assert dev.last_seen is None

    dev.last_seen = 0
    epoch = datetime(1970, 1, 1, 0, 0, 0, 0, tzinfo=timezone.utc)
    assert dev.last_seen == epoch.timestamp()

    dev.listener_event.assert_called_once_with("device_last_seen_updated", epoch)
    dev.listener_event.reset_mock()

    dev.update_last_seen()
    dev.listener_event.assert_called_once_with("device_last_seen_updated", ANY)
    event_time = dev.listener_event.mock_calls[0].args[1]
    assert (event_time - datetime.now(timezone.utc)).total_seconds() < 0.1


async def test_ignore_unknown_endpoint(dev, caplog):
    """Test that unknown endpoints are ignored."""
    dev.add_endpoint(1)

    with caplog.at_level(logging.DEBUG):
        dev.packet_received(
            t.ZigbeePacket(
                profile_id=260,
                cluster_id=1,
                src_ep=2,
                dst_ep=3,
                data=t.SerializableBytes(b"data"),
                src=t.AddrModeAddress(
                    addr_mode=t.AddrMode.NWK,
                    address=dev.nwk,
                ),
                dst=t.AddrModeAddress(
                    addr_mode=t.AddrMode.NWK,
                    address=0x0000,
                ),
            )
        )

    assert "Ignoring message on unknown endpoint" in caplog.text


async def test_deserialize_backwards_compat(dev):
    """Test that deserialization uses the method if it is overloaded."""
    packet = t.ZigbeePacket(
        profile_id=260,
        cluster_id=Basic.cluster_id,
        src_ep=1,
        dst_ep=1,
        data=t.SerializableBytes(
            b"\x18\x56\x09\x00\x00\x00\x00\x25\x1e\x00\x84\x03\x01\x02\x03\x04\x05\x06"
        ),
        src=t.AddrModeAddress(
            addr_mode=t.AddrMode.NWK,
            address=dev.nwk,
        ),
        dst=t.AddrModeAddress(
            addr_mode=t.AddrMode.NWK,
            address=0x0000,
        ),
    )

    ep = dev.add_endpoint(1)
    ep.add_input_cluster(Basic.cluster_id)

    dev.packet_received(packet)

    # Replace the method
    dev.deserialize = MagicMock(side_effect=dev.deserialize)
    dev.packet_received(packet)

    assert dev.deserialize.call_count == 1


async def test_request_exception_propagation(dev, event_loop):
    """Test that exceptions are propagated to the caller."""
    tsn = 0x12

    ep = dev.add_endpoint(1)
    ep.add_input_cluster(Basic.cluster_id)
    ep.deserialize = MagicMock(side_effect=RuntimeError())

    dev.application.get_sequence = MagicMock(return_value=tsn)

    event_loop.call_soon(
        dev.packet_received,
        t.ZigbeePacket(
            profile_id=260,
            cluster_id=Basic.cluster_id,
            src_ep=1,
            dst_ep=1,
            data=t.SerializableBytes(
                foundation.ZCLHeader(
                    frame_control=foundation.FrameControl(
                        frame_type=foundation.FrameType.CLUSTER_COMMAND,
                        is_manufacturer_specific=False,
                        direction=foundation.Direction.Client_to_Server,
                        disable_default_response=True,
                        reserved=0,
                    ),
                    tsn=tsn,
                    command_id=foundation.GeneralCommand.Default_Response,
                    manufacturer=None,
                ).serialize()
                + (
                    foundation.GENERAL_COMMANDS[
                        foundation.GeneralCommand.Default_Response
                    ]
                    .schema(
                        command_id=Basic.ServerCommandDefs.reset_fact_default.id,
                        status=foundation.Status.SUCCESS,
                    )
                    .serialize()
                )
            ),
            src=t.AddrModeAddress(
                addr_mode=t.AddrMode.NWK,
                address=dev.nwk,
            ),
            dst=t.AddrModeAddress(
                addr_mode=t.AddrMode.NWK,
                address=0x0000,
            ),
        ),
    )

    with pytest.raises(zigpy.exceptions.ParsingError) as exc:
        await ep.basic.reset_fact_default()

    assert type(exc.value.__cause__) is RuntimeError
