"""/api/network/* — interfaces and static addresses."""

import json

import pytest


def test_interfaces_come_from_the_profile_command(client, engine):
    links = [{"ifname": "lo"}, {"ifname": "fnas_lan", "address": "aa:bb:cc:dd:ee:ff"}]
    engine.outputs[("network", "list_interfaces")] = json.dumps(links)
    r = client.get("/api/network/interfaces")
    assert r.status == 200
    assert r.body == {"interfaces": links}
    [call] = engine.called("network", "list_interfaces")
    assert call.argv == ["ip", "-j", "addr", "show"]


@pytest.mark.parametrize("missing", ["ip", "cidr", "gateway", "dns"])
def test_a_static_address_needs_every_field(client, engine, missing):
    body = {"ip": "192.168.1.5", "cidr": 24, "gateway": "192.168.1.1", "dns": "1.1.1.1"}
    del body[missing]
    assert client.post("/api/network/interfaces/fnas_lan/ip", body).status == 422
    assert engine.actions() == []


def test_a_static_address_reaches_nmcli_and_the_config(client, engine, config):
    body = {"ip": "192.168.1.5", "cidr": 24, "gateway": "192.168.1.1",
            "dns": ["1.1.1.1", "9.9.9.9"]}
    r = client.post("/api/network/interfaces/fnas_lan/ip", body)
    assert r.status == 200
    [call] = engine.called("network", "set_static_ip")
    assert call.argv == [
        "nmcli", "connection", "modify", "fnas_lan",
        "ipv4.addresses", "192.168.1.5/24",
        "ipv4.gateway", "192.168.1.1",
        "ipv4.dns", "1.1.1.1 9.9.9.9",
        "ipv4.method", "manual",
    ]
    iface = config.get_interface("fnas_lan")
    assert iface["config"]["method"] == "static"
    assert iface["config"]["dns"] == ["1.1.1.1", "9.9.9.9"]
