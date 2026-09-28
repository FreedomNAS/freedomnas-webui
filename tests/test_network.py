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


def test_a_static_address_gets_its_own_networkmanager_connection(client, engine, config):
    body = {"ip": "192.168.1.5", "cidr": 24, "gateway": "192.168.1.1",
            "dns": ["1.1.1.1", "9.9.9.9"]}
    r = client.post("/api/network/interfaces/fnas_lan/ip", body)
    assert r.status == 200
    delete, add, up = engine.called("network", "set_static_ip")
    assert delete.command == "nmcli connection delete fnas-fnas_lan || true"
    assert add.argv == [
        "nmcli", "connection", "add", "type", "ethernet", "ifname", "fnas_lan",
        "con-name", "fnas-fnas_lan", "connection.autoconnect-priority", "100",
        "ipv4.method", "manual", "ipv4.addresses", "192.168.1.5/24",
        "ipv4.gateway", "192.168.1.1", "ipv4.dns", "1.1.1.1 9.9.9.9",
    ]
    assert up.argv == ["nmcli", "connection", "up", "fnas-fnas_lan"]
    iface = config.get_interface("fnas_lan")
    assert iface["config"]["method"] == "static"
    assert iface["config"]["dns"] == ["1.1.1.1", "9.9.9.9"]


def test_a_hostile_interface_name_stays_one_word_in_the_shell_step(client, engine):
    body = {"ip": "192.168.1.5", "cidr": 24, "gateway": "192.168.1.1", "dns": []}
    client.post("/api/network/interfaces/x;reboot/ip", body)
    delete = engine.called("network", "set_static_ip")[0]
    assert delete.command == "nmcli connection delete fnas-'x;reboot' || true"
